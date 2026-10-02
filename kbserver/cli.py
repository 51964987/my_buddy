"""命令行投递入口（§7 P0 判据：命令行投递 URL）。

用法：
  python -m kbserver.cli submit <url> [--title T] [--text "..."]
  python -m kbserver.cli status
可选：--base http://127.0.0.1:8765  --token TOKEN
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib import error as _urlerror
from urllib import request as _urlrequest


def _request(base: str, method: str, path: str, payload: dict | None, token: str) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-KB-Token"] = token
    req = _urlrequest.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    with _urlrequest.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", default="http://127.0.0.1:8765", help="service base url")
    common.add_argument("--token", default="", help="auth token if configured")

    parser = argparse.ArgumentParser(prog="python -m kbserver.cli", description="kbserver capture CLI", parents=[common])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_submit = sub.add_parser("submit", help="submit a URL or text note", parents=[common])
    p_submit.add_argument("url", nargs="?", help="page url (http/https)")
    p_submit.add_argument("--text", default=None, help="text note or selected text")
    p_submit.add_argument("--title", default=None, help="page title")

    sub.add_parser("status", help="show pipeline status", parents=[common])

    args = parser.parse_args(argv)

    try:
        if args.cmd == "submit":
            if not args.url and not args.text:
                parser.error("submit requires a URL or --text")
            payload = {"url": args.url, "text": args.text, "title": args.title, "entry": "cli"}
            status, body = _request(args.base, "POST", "/api/capture", payload, args.token)
        else:
            status, body = _request(args.base, "GET", "/api/status", None, args.token)
    except _urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except _urlerror.URLError as exc:
        print(f"connection failed: {exc.reason}", file=sys.stderr)
        return 1

    print(json.dumps(body, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
