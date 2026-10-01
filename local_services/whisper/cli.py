from __future__ import annotations
import argparse
import json
import sys

from bridge_core import health, load_model, read_transcript, transcribe_mode

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

def main() -> None:
    p = argparse.ArgumentParser(prog="whisper-tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")

    t = sub.add_parser("transcribe")
    t.add_argument("path")
    t.add_argument("--language", default="zh")
    t.add_argument("--mode", choices=["quality", "balanced", "fast"], default="quality")
    t.add_argument("--prompt", default="")
    t.add_argument("--carry-initial-prompt", action="store_true")

    m = sub.add_parser("model")
    m.add_argument("filename")

    r = sub.add_parser("read")
    r.add_argument("json_path")
    r.add_argument("--start", type=float, default=0)
    r.add_argument("--end", type=float)
    r.add_argument("--max-chars", type=int, default=12000)

    a = p.parse_args()
    if a.cmd == "health":
        result = health()
    elif a.cmd == "transcribe":
        result = transcribe_mode(
            a.path, language=a.language, mode=a.mode,
            prompt=a.prompt, carry_initial_prompt=a.carry_initial_prompt,
        )
    elif a.cmd == "model":
        result = load_model(a.filename)
    else:
        result = read_transcript(a.json_path, a.start, a.end, a.max_chars)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
