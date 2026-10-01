from __future__ import annotations
import argparse
import json

from service_core import correct_text, health, start_server, stop_server

def main() -> None:
    p = argparse.ArgumentParser(prog="llama-tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")

    s = sub.add_parser("start")
    s.add_argument("--profile", default="")

    sub.add_parser("stop")

    c = sub.add_parser("correct")
    c.add_argument("text")
    c.add_argument("--context", default="")
    c.add_argument("--glossary", default="")
    c.add_argument("--profile", default="")
    c.add_argument("--max-tokens", type=int, default=4096)

    a = p.parse_args()
    if a.cmd == "health":
        result = health()
    elif a.cmd == "start":
        result = start_server(a.profile or None)
    elif a.cmd == "stop":
        result = stop_server()
    else:
        result = correct_text(
            a.text, a.context, a.glossary,
            a.profile or None, a.max_tokens,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
