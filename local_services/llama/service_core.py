from __future__ import annotations
import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("PIPELINE_LLAMA_CONFIG", ROOT / "config" / "config.json"))

def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"llama config is missing: {CONFIG_PATH}. Copy config.example.json to config.json and edit its paths."
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key in ("server_exe", "api_key_file", "log_dir", "output_dir"):
        value = cfg.get(key)
        if value:
            path = Path(value).expanduser()
            cfg[key] = str(path.resolve() if path.is_absolute() else (ROOT / path).resolve())
    api_key = Path(cfg["api_key_file"])
    api_key.parent.mkdir(parents=True, exist_ok=True)
    if not api_key.exists():
        api_key.write_text(secrets.token_urlsafe(32), encoding="ascii")
    return cfg

def _api_key(cfg: dict) -> str:
    return Path(cfg["api_key_file"]).read_text(encoding="ascii").strip()

def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {_api_key(cfg)}"}

def _base_url(cfg: dict) -> str:
    return f"http://{cfg['host']}:{cfg['port']}"

def _pid_path(cfg: dict) -> Path:
    return Path(cfg["log_dir"]) / "llama-server.pid"
def _profile_path(cfg: dict) -> Path:
    return Path(cfg["log_dir"]) / "active-profile.txt"

def _read_pid(cfg: dict) -> int | None:
    p = _pid_path(cfg)
    if not p.is_file():
        return None
    try:
        return int(p.read_text(encoding="ascii").strip())
    except Exception:
        return None

def _read_profile(cfg: dict) -> str | None:
    p = _profile_path(cfg)
    return p.read_text(encoding="utf-8").strip() if p.is_file() else None

def health(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    try:
        r = httpx.get(
            f"{_base_url(cfg)}/health",
            headers=_headers(cfg),
            timeout=3.0,
            trust_env=False,
        )
        r.raise_for_status()
        data = r.json()
        data["active_profile"] = _read_profile(cfg)
        return data
    except Exception as exc:
        return {
            "status": "down",
            "detail": str(exc),
            "active_profile": _read_profile(cfg),
        }

def stop_server(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    pid = _read_pid(cfg)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    _pid_path(cfg).unlink(missing_ok=True)
    _profile_path(cfg).unlink(missing_ok=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        if health(cfg).get("status") != "ok":
            return {"status": "stopped", "pid": pid}
        time.sleep(0.25)
    return {"status": "unknown", "pid": pid}

def start_server(profile: str | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    profile = profile or cfg["default_profile"]
    if profile not in cfg["profiles"]:
        raise ValueError(f"Unknown profile: {profile}")

    current = health(cfg)
    if current.get("status") == "ok" and _read_profile(cfg) == profile:
        return current
    if current.get("status") == "ok":
        stop_server(cfg)

    model = cfg["profiles"][profile]
    log_dir = Path(cfg["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "llama-server.log", "a", encoding="utf-8", errors="replace")

    cmd = [
        cfg["server_exe"],
        "-hf", model,
        "-ngl", str(cfg.get("n_gpu_layers", 0)),
        "-c", str(cfg["ctx_size"]),
        "-fa", "auto",
        "-np", str(cfg["parallel"]),
        "--reasoning", "off",
        "--cors-origins", "localhost",
        "--api-key-file", cfg["api_key_file"],
        "--host", cfg["host"],
        "--port", str(cfg["port"]),
    ]
    if cfg.get("device"):
        cmd[cmd.index("-c"):cmd.index("-c")] = ["-dev", cfg["device"]]
    flags = 0
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
        )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        creationflags=flags,
        close_fds=True,
    )
    _pid_path(cfg).write_text(str(proc.pid), encoding="ascii")
    _profile_path(cfg).write_text(profile, encoding="utf-8")

    deadline = time.time() + 180
    while time.time() < deadline:
        state = health(cfg)
        if state.get("status") == "ok":
            state["profile"] = profile
            state["model"] = model
            state["pid"] = proc.pid
            return state
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited with code {proc.returncode}")
        time.sleep(0.5)
    raise RuntimeError("llama-server did not become healthy")

def ensure_server(profile: str | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    profile = profile or cfg["default_profile"]
    state = health(cfg)
    if state.get("status") == "ok" and _read_profile(cfg) == profile:
        return state
    return start_server(profile, cfg)

CORRECTION_SYSTEM = """你是ASR纠错审计器。
你不能重写全文，只能提出必要的最小字符串替换。
输出严格 JSON 对象，且只能有以下结构：
{"edits":[{"from":"原文中的错误字符串","to":"修正字符串","confidence":0.99,"reason":"简短原因"}]}

规则：
1. from 必须逐字出现在“待纠错文本”中，并尽可能短且唯一。
2. 只修正由发音、局部上下文、专名、固定表达或给定词表高置信度确定的 ASR 错误。
3. 禁止同义词替换、润色、语序优化、标点风格化、总结、扩写、事实审查。
4. 原文本身正确或只是表达生硬时，不得提出修改。
5. 候选术语/专名只是提示，不能强行套用。
6. 没有高置信度错误时返回 {"edits":[]}。
7. confidence 必须是 0 到 1 之间的数字。
/no_think"""
def _parse_edit_proposal(content: str) -> list[dict]:
    raw = (content or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"Model did not return JSON: {raw[:300]}")
        obj = json.loads(raw[start:end + 1])
    edits = obj.get("edits", [])
    if not isinstance(edits, list):
        raise ValueError("JSON field 'edits' must be a list")
    return [e for e in edits if isinstance(e, dict)]


def _apply_edit_proposals(
    text: str,
    proposals: list[dict],
    min_confidence: float,
) -> tuple[str, list[dict], list[dict]]:
    candidates = []
    skipped = []
    for edit in proposals:
        src = str(edit.get("from", ""))
        dst = str(edit.get("to", ""))
        try:
            confidence = float(edit.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        record = {
            "from": src,
            "to": dst,
            "confidence": confidence,
            "reason": str(edit.get("reason", "")),
        }
        if not src or src == dst:
            skipped.append({**record, "skip_reason": "empty_or_noop"})
            continue
        if confidence < min_confidence:
            skipped.append({**record, "skip_reason": "low_confidence"})
            continue
        if text.count(src) != 1:
            skipped.append({**record, "skip_reason": "source_not_unique"})
            continue
        start = text.find(src)
        candidates.append({**record, "start": start, "end": start + len(src)})

    candidates.sort(key=lambda x: (-x["confidence"], -(x["end"] - x["start"])))
    accepted = []
    for item in candidates:
        overlap = any(
            item["start"] < kept["end"] and item["end"] > kept["start"]
            for kept in accepted
        )
        if overlap:
            skipped.append({**item, "skip_reason": "overlap"})
        else:
            accepted.append(item)

    accepted.sort(key=lambda x: x["start"])
    out = []
    cursor = 0
    applied = []
    for item in accepted:
        out.append(text[cursor:item["start"]])
        out.append(item["to"])
        cursor = item["end"]
        applied.append({k: v for k, v in item.items() if k not in ("start", "end")})
    out.append(text[cursor:])
    return "".join(out), applied, skipped


def correct_text(
    text: str,
    context: str = "",
    glossary: str = "",
    profile: str | None = None,
    max_tokens: int = 4096,
    min_confidence: float = 0.85,
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or load_config()
    profile = profile or cfg["default_profile"]
    ensure_server(profile, cfg)

    extra = []
    if context.strip():
        extra.append("上下文：\n" + context.strip())
    if glossary.strip():
        extra.append("候选术语/专名：\n" + glossary.strip())
    user = "\n\n".join(extra + ["待纠错文本：\n" + text.strip()])

    payload = {
        "model": cfg["profiles"][profile],
        "messages": [
            {"role": "system", "content": CORRECTION_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    with httpx.Client(timeout=None, trust_env=False) as client:
        r = client.post(
            f"{_base_url(cfg)}/v1/chat/completions",
            headers={**_headers(cfg), "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 400:
            payload.pop("response_format", None)
            r = client.post(
                f"{_base_url(cfg)}/v1/chat/completions",
                headers={**_headers(cfg), "Content-Type": "application/json"},
                json=payload,
            )
        r.raise_for_status()
        data = r.json()

    message = data["choices"][0]["message"]
    proposals = _parse_edit_proposal(message.get("content", ""))
    corrected, applied, skipped = _apply_edit_proposals(
        text=text,
        proposals=proposals,
        min_confidence=min_confidence,
    )
    return {
        "profile": profile,
        "model": cfg["profiles"][profile],
        "original": text,
        "corrected": corrected,
        "proposed_edits": proposals,
        "applied_edits": applied,
        "skipped_edits": skipped,
        "min_confidence": min_confidence,
        "reasoning": message.get("reasoning_content"),
        "usage": data.get("usage", {}),
        "timings": data.get("timings", {}),
    }
