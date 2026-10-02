"""Download a URL and produce raw and corrected Chinese transcripts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse


MEDIA_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".flv", ".m4v", ".avi", ".ogv", ".mp3", ".m4a", ".aac", ".wav", ".wma", ".oga", ".ogg", ".opus", ".weba", ".m3u8", ".m3u", ".mpd"}
OUTPUT_EXTENSIONS = MEDIA_EXTENSIONS - {".m3u8", ".m3u", ".mpd"}
PUNCTUATION = set("。！？；.!?;\n")


class PipelineError(RuntimeError):
    pass


def is_direct_media_url(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in MEDIA_EXTENSIONS


def choose_candidates(resources: list[dict]) -> list[dict]:
    usable = [r for r in resources if r.get("category") in {"video", "audio", "hls", "dash"}]

    def rank(resource: dict) -> tuple[int, int, int]:
        size = resource.get("size")
        known_file = resource["category"] in {"video", "audio"} and isinstance(size, (int, float)) and size > 0
        manifest = resource["category"] in {"hls", "dash"}
        return (2 if known_file else 1 if manifest else 0, int(size or 0) if known_file else 0, -int(resource["id"]))

    return sorted(usable, key=rank, reverse=True)


def choose_manual_resource(resources: list[dict], resource_id: int | None, interactive: bool) -> dict:
    if resource_id is None:
        if not interactive:
            raise PipelineError("Manual selection without a terminal requires --resource-id")
        for resource in resources:
            size = resource.get("size")
            print(f'{resource["id"]:>3}  {resource["category"]:<6}  {size if size is not None else "unknown":>10}  {resource["url"]}')
        try:
            resource_id = int(input("Resource ID: ").strip())
        except (ValueError, EOFError) as exc:
            raise PipelineError("Enter a valid resource ID") from exc
    for resource in resources:
        if resource["id"] == resource_id:
            return resource
    raise PipelineError(f"Resource ID {resource_id} was not found")


def split_text(value: str, max_chars: int = 1500) -> list[str]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    chunks = []
    start = 0
    while start < len(value):
        end = min(start + max_chars, len(value))
        if end < len(value):
            boundary = next((i + 1 for i in range(end - 1, start + max_chars // 2 - 1, -1) if value[i] in PUNCTUATION), None)
            if boundary is not None:
                end = boundary
        chunks.append(value[start:end])
        start = end
    return chunks


def _invoke(runner, args: list[str], extra_env: dict[str, str] | None = None) -> str:
    try:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        if extra_env:
            environment.update(extra_env)
        result = runner(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, env=environment)
    except OSError as exc:
        raise PipelineError(f"Cannot start {args[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-2000:]
        raise PipelineError(f"{Path(str(args[0])).name} failed ({result.returncode}): {detail}")
    return result.stdout or ""


def _json_result(runner, args: list[str], extra_env: dict[str, str] | None = None) -> dict:
    output = _invoke(runner, args, extra_env)
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Invalid JSON from {Path(str(args[0])).name}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"Unexpected JSON from {Path(str(args[0])).name}")
    return value


def _service_python(directory: Path, fallback: str) -> str:
    candidate = directory / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else fallback


def _source_suffix(resource: dict) -> str:
    if resource.get("category") in {"hls", "dash"}:
        return ".mkv"
    suffix = Path(urlparse(resource["url"]).path).suffix.lower()
    return suffix if suffix in OUTPUT_EXTENSIONS else ".mkv"


def _correct_chunk(runner, args: list[str], env: dict[str, str], chunk: str, depth: int = 0) -> dict:
    try:
        return _json_result(runner, args, env)
    except PipelineError as exc:
        if depth >= 3 or len(chunk) < 200 or "JSONDecodeError" not in str(exc):
            raise
        # A model response can hit its token limit before closing the edit JSON.
        parts = split_text(chunk, max_chars=(len(chunk) + 1) // 2)
        results = []
        for part in parts:
            retry_args = list(args)
            retry_args[retry_args.index("correct") + 1] = part
            results.append(_correct_chunk(runner, retry_args, env, part, depth + 1))
        if any(not isinstance(result.get("corrected"), str) for result in results):
            raise PipelineError("Correction retry returned no text")
        return {"corrected": "".join(result["corrected"] for result in results),
                "applied_edits": [edit for result in results for edit in result.get("applied_edits", [])]}


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""))


def _record_resource(resource: dict) -> dict:
    record = {key: resource.get(key) for key in ("id", "category", "size", "mimeType", "url") if key in resource}
    if "url" in record:
        record["url"] = _safe_url(record["url"])
    return record


def run_pipeline(
    url: str, *, output_root: Path, cat_dir: Path,
    whisper_dir: Path | None = None, llama_dir: Path | None = None,
    selection: str = "auto", resource_id: int | None = None, cookies: Path | None = None,
    storage_state: Path | None = None, duration: float | None = None, clicks: list[str] | None = None,
    wait: float = 15, whisper_mode: str = "quality", llama_profile: str = "quality",
    runner=subprocess.run, python_executable: str | None = None, node_executable: str = "node",
    ffmpeg_executable: str = "ffmpeg", interactive: bool | None = None,
) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PipelineError("URL must be HTTP or HTTPS")
    if selection not in {"auto", "manual"}:
        raise PipelineError("Selection must be auto or manual")
    if cookies and storage_state:
        raise PipelineError("--cookies and --storage-state cannot be combined")
    if duration is not None and duration <= 0:
        raise PipelineError("--duration must be positive")
    if resource_id is not None and resource_id < 1:
        raise PipelineError("--resource-id must be positive")

    project_root = Path(__file__).resolve().parent
    cat_dir = Path(cat_dir)
    whisper_dir = Path(whisper_dir) if whisper_dir else project_root / "local_services" / "whisper"
    llama_dir = Path(llama_dir) if llama_dir else project_root / "local_services" / "llama"
    output_root = Path(output_root)
    cat_cli = cat_dir / "cli" / "bin.js"
    whisper_cli = whisper_dir / "cli.py"
    llama_cli = llama_dir / "cli.py"
    for label, path in (("cat-catch", cat_cli), ("Whisper service", whisper_cli), ("llama service", llama_cli)):
        if runner is subprocess.run and not path.is_file():
            raise PipelineError(f"{label} CLI was not found: {path}")
    if runner is subprocess.run:
        for service, directory in (("Whisper", whisper_dir), ("llama", llama_dir)):
            config = directory / "config" / "config.json"
            if not config.is_file():
                raise PipelineError(f"{service} config is missing: {config}. Run setup.ps1 or copy config.example.json to config.json.")

    run_dir = output_root / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    py = python_executable or sys.executable
    resources = []
    sniff_error = None
    direct = is_direct_media_url(url)
    if direct:
        if cookies or storage_state:
            raise PipelineError("Cookie/state files apply to webpage capture; use the webpage URL for authenticated media")
        resources = [{"id": 1, "category": "hls" if parsed.path.lower().endswith((".m3u8", ".m3u")) else "dash" if parsed.path.lower().endswith(".mpd") else "audio" if Path(parsed.path).suffix.lower() in {".mp3", ".m4a", ".aac", ".wav", ".wma", ".oga", ".ogg", ".opus", ".weba"} else "video", "size": None, "url": url}]
    else:
        sniff = [node_executable, str(cat_cli), "sniff", url, "--format", "json", "--wait", str(wait)]
        if cookies:
            sniff += ["--cookies", str(cookies)]
        if storage_state:
            sniff += ["--storage-state", str(storage_state)]
        sniff.append("--include-headers")
        for click in clicks or []:
            sniff += ["--click", click]
        try:
            capture = _json_result(runner, sniff)
            if capture.get("schemaVersion") != 1 or not isinstance(capture.get("resources"), list):
                raise PipelineError("Unsupported cat-catch capture data")
            resources = choose_candidates(capture["resources"])
        except PipelineError as exc:
            if cookies or storage_state or clicks:
                raise
            sniff_error = str(exc)
        if not resources:
            # A signed media endpoint may have no filename extension and may
            # refuse browser navigation while still supporting a direct GET.
            direct = True
            resources = [{"id": 1, "category": "video", "size": None, "url": url}]
    if not resources:
        raise PipelineError("No downloadable video or audio resources were found")

    if resource_id is not None or selection == "manual":
        selected = choose_manual_resource(resources, resource_id, sys.stdin.isatty() if interactive is None else interactive)
        candidates = [selected]
    else:
        candidates = choose_candidates(resources)

    attempts = []
    selected = None
    source = None
    audio = run_dir / "audio.wav"
    for candidate in candidates:
        source = run_dir / f'source-{candidate["id"]}{_source_suffix(candidate)}'
        download = [node_executable, str(cat_cli), "download"]
        capture_path = None
        if direct:
            download.append(candidate["url"])
        else:
            capture_path = run_dir / "capture.tmp.json"
            download_resource = dict(candidate)
            excluded = {"host", "content-length", "range", "connection", "accept-encoding"}
            download_resource["headers"] = {
                name: value for name, value in candidate.get("headers", {}).items()
                if not name.startswith(":") and name.lower() not in excluded
            }
            capture_path.write_text(json.dumps({"schemaVersion": 1, "resources": [download_resource]}, ensure_ascii=False), encoding="utf-8")
            download += ["--from", str(capture_path), "--id", str(candidate["id"])]
        download += ["-o", str(source), "--ffmpeg", ffmpeg_executable]
        if duration is not None:
            download += ["--duration", str(duration)]
        try:
            _invoke(runner, download)
            _invoke(runner, [ffmpeg_executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)])
            if not audio.is_file() or audio.stat().st_size == 0:
                raise PipelineError("FFmpeg produced no audio")
            selected = candidate
            break
        except PipelineError as exc:
            attempts.append({"resource": _record_resource(candidate), "error": str(exc)})
            audio.unlink(missing_ok=True)
            if selection == "manual" or resource_id is not None:
                break
        finally:
            if capture_path is not None:
                capture_path.unlink(missing_ok=True)
    if selected is None:
        (run_dir / "attempts.json").write_text(json.dumps(attempts, ensure_ascii=False, indent=2), encoding="utf-8")
        detail = f" Sniff error: {sniff_error}" if sniff_error else ""
        raise PipelineError(f"No candidate yielded usable audio; attempts saved in {run_dir / 'attempts.json'}.{detail}")

    record = {"input_url": _safe_url(url), "selection": selection, "selected_resource": _record_resource(selected), "attempts": attempts, "whisper_mode": whisper_mode, "llama_profile": llama_profile}
    (run_dir / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    whisper_python = _service_python(whisper_dir, py)
    whisper_env = {"WHISPER_BRIDGE_CONFIG": str(whisper_dir / "config" / "config.json")}
    transcription = _json_result(runner, [whisper_python, str(whisper_cli), "transcribe", str(audio), "--language", "zh", "--mode", whisper_mode], whisper_env)
    files = transcription.get("files") or {}
    for ext in ("json", "txt", "srt"):
        source_file = Path(files.get(ext, ""))
        if not source_file.is_file():
            raise PipelineError(f"Whisper did not produce raw {ext.upper()}: {source_file}")
        shutil.copy2(source_file, run_dir / f"raw.{ext}")
    correct_saved_transcript(run_dir, llama_dir, llama_profile, runner, py)
    return {"run_dir": run_dir, "selected_resource": _record_resource(selected)}


def correct_saved_transcript(run_dir: Path, llama_dir: Path, llama_profile: str = "quality",
                             runner=subprocess.run, py: str | None = None) -> None:
    llama_cli = llama_dir / "cli.py"
    raw = (run_dir / "raw.txt").read_text(encoding="utf-8")
    corrections = []
    corrected_chunks = []
    llama_python = _service_python(llama_dir, py or sys.executable)
    chunks = split_text(raw)
    for index, chunk in enumerate(chunks):
        context = "".join(chunks[max(0, index - 1):index])[-300:]
        args = [llama_python, str(llama_cli), "correct", chunk, "--profile", llama_profile]
        if context:
            args += ["--context", context]
        try:
            result = _correct_chunk(runner, args, {"PIPELINE_LLAMA_CONFIG": str(llama_dir / "config" / "config.json")}, chunk)
        except PipelineError as exc:
            (run_dir / "corrections.partial.json").write_text(json.dumps(corrections, ensure_ascii=False, indent=2), encoding="utf-8")
            raise PipelineError(f"Correction chunk {index + 1} failed: {exc}") from exc
        corrected = result.get("corrected")
        if not isinstance(corrected, str):
            raise PipelineError(f"Correction chunk {index + 1} returned no text")
        corrected_chunks.append(corrected)
        corrections.append({"chunk": index + 1, "original": chunk, "corrected": corrected, "applied_edits": result.get("applied_edits", [])})
    (run_dir / "corrected.txt").write_text("".join(corrected_chunks), encoding="utf-8")
    (run_dir / "corrections.json").write_text(json.dumps(corrections, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "corrections.partial.json").unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download a video URL and produce a corrected Chinese transcript")
    parser.add_argument("url")
    parser.add_argument("--selection", choices=["auto", "manual"], default="auto")
    parser.add_argument("--resource-id", type=int)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "outputs")
    parser.add_argument("--cat-catch-dir", type=Path, default=Path(__file__).resolve().parent / "third_party" / "cat-catch")
    parser.add_argument("--whisper-service-dir", type=Path, default=Path(__file__).resolve().parent / "local_services" / "whisper")
    parser.add_argument("--llama-service-dir", type=Path, default=Path(__file__).resolve().parent / "local_services" / "llama")
    parser.add_argument("--cookies", type=Path)
    parser.add_argument("--storage-state", type=Path)
    parser.add_argument("--click", action="append", default=[])
    parser.add_argument("--wait", type=float, default=15)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--whisper-mode", choices=["quality", "balanced", "fast"], default="quality")
    parser.add_argument("--llama-profile", default="quality")
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(args.url, output_root=args.output_dir, cat_dir=args.cat_catch_dir,
                              whisper_dir=args.whisper_service_dir, llama_dir=args.llama_service_dir,
                              selection=args.selection, resource_id=args.resource_id, cookies=args.cookies,
                              storage_state=args.storage_state, clicks=args.click, wait=args.wait,
                              duration=args.duration, whisper_mode=args.whisper_mode,
                              llama_profile=args.llama_profile)
    except PipelineError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(result["run_dir"] / "corrected.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
