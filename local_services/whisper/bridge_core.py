from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt

import httpx

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("WHISPER_BRIDGE_CONFIG", ROOT / "config" / "config.json"))

def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"Whisper config is missing: {CONFIG_PATH}. Copy config.example.json to config.json and edit its paths."
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key in ("server_exe", "models_dir", "output_dir", "log_dir", "ffmpeg_exe"):
        value = cfg.get(key)
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute() and ("/" in value or "\\" in value or key != "ffmpeg_exe"):
            path = (ROOT / path).resolve()
        elif key == "ffmpeg_exe" and not path.is_absolute():
            path = Path(shutil.which(value) or value)
        cfg[key] = str(path)
    cfg["allowed_input_roots"] = [
        str((ROOT / Path(value).expanduser()).resolve()) if not Path(value).expanduser().is_absolute() else str(Path(value).expanduser().resolve())
        for value in cfg.get("allowed_input_roots", ["../.."])
    ]
    return cfg

def mode_catalog(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    return cfg["modes"]

def _resolved_roots(cfg: dict) -> list[Path]:
    return [Path(x).expanduser().resolve() for x in cfg["allowed_input_roots"]]

def allowed_path(path: str, cfg: dict) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(str(p))
    roots = _resolved_roots(cfg)
    if not any(p == root or root in p.parents for root in roots):
        raise PermissionError(f"Input path is outside allowed roots: {p}")
    return p

def server_url(cfg: dict) -> str:
    return f"http://{cfg['host']}:{cfg['port']}"

def health(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    try:
        r = httpx.get(f"{server_url(cfg)}/health", timeout=3.0, trust_env=False)
        r.raise_for_status()
        result = r.json()
        result["active_model"] = _read_active_model(cfg)
        return result
    except Exception as exc:
        return {"status": "down", "detail": str(exc), "active_model": None}

def ensure_server(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    current = health(cfg)
    if current.get("status") == "ok":
        return current

    exe = Path(cfg["server_exe"])
    model = Path(cfg["models_dir"]) / cfg["default_model"]
    if not exe.is_file():
        raise FileNotFoundError(str(exe))
    if not model.is_file():
        raise FileNotFoundError(str(model))
    log_dir = Path(cfg["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = log_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "whisper-server.log", "a", encoding="utf-8", errors="replace")

    cmd = [
        str(exe), "-m", str(model), "-l", cfg["language"],
        "-dev", str(cfg["gpu_device"]), "-fa", "--convert",
        "--host", cfg["host"], "--port", str(cfg["port"]),
        "--tmp-dir", str(tmp_dir),
    ]
    flags = 0
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
        )
    env = os.environ.copy()
    ffmpeg_dir = str(Path(cfg["ffmpeg_exe"]).resolve().parent)
    env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
    subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        creationflags=flags, close_fds=True, env=env,
    )

    deadline = time.time() + 90
    while time.time() < deadline:
        current = health(cfg)
        if current.get("status") == "ok":
            _write_active_model(cfg["default_model"], cfg)
            return current
        time.sleep(0.5)
    raise RuntimeError("whisper-server did not become healthy within 90 seconds")

def _active_model_path(cfg: dict) -> Path:
    return Path(cfg["log_dir"]) / "active-model.txt"

def _read_active_model(cfg: dict) -> str | None:
    p = _active_model_path(cfg)
    return p.read_text(encoding="utf-8").strip() if p.is_file() else None

def _write_active_model(name: str, cfg: dict) -> None:
    p = _active_model_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(name, encoding="utf-8")

@contextmanager
def service_lock(cfg: dict):
    if os.name != "nt":
        yield
        return
    p = Path(cfg["log_dir"]) / "service.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.25)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

def load_model(model_filename: str, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    ensure_server(cfg)
    name = Path(model_filename).name
    if name != model_filename:
        raise ValueError("model_filename must be a basename, not a path")
    model = (Path(cfg["models_dir"]) / name).resolve()
    if not model.is_file():
        raise FileNotFoundError(str(model))
    if _read_active_model(cfg) == name:
        return {"status": "ok", "model": name, "changed": False}

    with httpx.Client(timeout=180.0, trust_env=False) as client:
        r = client.post(
            f"{server_url(cfg)}/load",
            files={"model": (None, str(model))},
        )
        r.raise_for_status()
    _write_active_model(name, cfg)
    return {
        "status": "ok",
        "model": name,
        "changed": True,
        "server_response": r.text.strip(),
    }

def _timestamp(seconds: float) -> str:
    ms = round(float(seconds) * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def _to_srt(segments: list[dict]) -> str:
    blocks = []
    for i, seg in enumerate(segments, 1):
        blocks.append(
            f"{i}\n{_timestamp(seg.get('start', 0))} --> "
            f"{_timestamp(seg.get('end', 0))}\n"
            f"{seg.get('text', '').strip()}\n"
        )
    return "\n".join(blocks)

@contextmanager
def media_for_upload(source: Path, cfg: dict):
    video_exts = {
        ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv",
    }
    should_compress = (
        source.suffix.lower() in video_exts
        or source.stat().st_size > 128 * 1024 * 1024
    )
    if not should_compress:
        yield source
        return

    tmp_dir = Path(cfg["log_dir"]) / "upload-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{source.stem}-{uuid.uuid4().hex[:8]}.flac"
    cmd = [
        cfg["ffmpeg_exe"], "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-map", "0:a:0", "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "flac",
        "-compression_level", "5", str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True)
        yield tmp
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

def transcribe(
    path: str,
    language: str | None = None,
    prompt: str = "",
    carry_initial_prompt: bool = False,
    profile: str = "accurate",
    save: bool = True,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or load_config()
    source = allowed_path(path, cfg)
    ensure_server(cfg)

    profiles = {
        "fast": {"beam_size": "-1", "best_of": "2"},
        "accurate": {"beam_size": "5", "best_of": "5"},
    }
    if profile not in profiles:
        raise ValueError(f"Unknown profile: {profile}. Use 'fast' or 'accurate'.")
    form = {
        "language": language or cfg["language"],
        "response_format": "verbose_json",
        **profiles[profile],
    }
    if prompt:
        form["prompt"] = prompt
        form["carry_initial_prompt"] = (
            "true" if carry_initial_prompt else "false"
        )
    started = time.perf_counter()
    preprocessed = False
    with media_for_upload(source, cfg) as upload:
        preprocessed = upload != source
        with upload.open("rb") as f, httpx.Client(timeout=None, trust_env=False) as client:
            r = client.post(
                f"{server_url(cfg)}/inference",
                data=form,
                files={"file": (upload.name, f, "application/octet-stream")},
            )
            r.raise_for_status()
            data = r.json()
    elapsed = time.perf_counter() - started

    text = data.get("text", "").strip()
    segments = data.get("segments", [])
    duration = float(data.get("duration", 0) or 0)
    files: dict[str, str] = {}

    if save:
        out_dir = Path(cfg["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        job = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        base = out_dir / f"{source.stem}-{job}"
        json_path = base.with_suffix(".json")
        txt_path = base.with_suffix(".txt")
        srt_path = base.with_suffix(".srt")
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        txt_path.write_text(text + "\n", encoding="utf-8")
        srt_path.write_text(_to_srt(segments), encoding="utf-8")
        files = {
            "json": str(json_path),
            "txt": str(txt_path),
            "srt": str(srt_path),
        }

    return {
        "source": str(source),
        "language": data.get("language", language or cfg["language"]),
        "duration_seconds": round(duration, 3),
        "elapsed_seconds": round(elapsed, 3),
        "realtime_x": round(duration / elapsed, 2) if elapsed and duration else None,
        "segments": len(segments),
        "text_chars": len(text),
        "profile": profile,
        "preprocessed_for_upload": preprocessed,
        "files": files,
        "preview": text[:1500],
        "_raw": data if not save else None,
    }

def transcribe_mode(
    path: str,
    language: str = "zh",
    mode: str = "quality",
    prompt: str = "",
    carry_initial_prompt: bool = False,
    save: bool = True,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or load_config()
    modes = mode_catalog(cfg)
    if mode not in modes:
        raise ValueError(
            f"Unknown mode: {mode}. Use one of: {', '.join(modes)}"
        )
    spec = modes[mode]
    with service_lock(cfg):
        load_model(spec["model"], cfg)
        result = transcribe(
            path,
            language=language,
            prompt=prompt,
            carry_initial_prompt=carry_initial_prompt,
            profile=spec["profile"],
            save=save,
            cfg=cfg,
        )
    result["mode"] = mode
    result["model"] = spec["model"]
    return result

def read_transcript(
    json_path: str,
    start_seconds: float = 0,
    end_seconds: float | None = None,
    max_chars: int = 12000,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or load_config()
    p = Path(json_path).expanduser().resolve()
    output_root = Path(cfg["output_dir"]).resolve()
    if output_root not in p.parents:
        raise PermissionError("Transcript must be inside the configured output directory")
    data = json.loads(p.read_text(encoding="utf-8"))

    lines = []
    for seg in data.get("segments", []):
        start = float(seg.get("start", 0))
        end = float(seg.get("end", 0))
        if end < start_seconds:
            continue
        if end_seconds is not None and start > end_seconds:
            continue
        lines.append(f"[{start:8.2f}-{end:8.2f}] {seg.get('text', '').strip()}")

    full = "\n".join(lines)
    cap = max(1000, min(int(max_chars), 50000))
    return {
        "path": str(p),
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "text": full[:cap],
        "truncated": len(full) > cap,
    }
