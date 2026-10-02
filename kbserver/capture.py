"""Capture API 受理逻辑 + inbox 队列落盘（§11.4 前台接待员 / 暂存区管理员）。

只校验与受理，不抓取、不判重、不转格式；原始 payload 原样保存。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .guard import Guard
from .idgen import canonicalize_url, text_entry_id, url_to_id

ENTRY_CHANNELS = ("browser_ext", "mobile_share", "crawler", "collab_api", "cli")


class CaptureError(ValueError):
    pass


def provisional_id(url: str | None) -> str:
    if url:
        return url_to_id(canonicalize_url(url))
    return text_entry_id()


def accept_capture(guard: Guard, payload: dict[str, Any]) -> dict:
    entry = (payload.get("entry") or "cli").strip()
    if entry not in ENTRY_CHANNELS:
        raise CaptureError(f"unknown entry channel: {entry}")

    url = (payload.get("url") or "").strip()
    text = (payload.get("text") or payload.get("selected_text") or "").strip()
    if url:
        if not url.lower().startswith(("http://", "https://")):
            raise CaptureError("url must start with http:// or https://")
    elif not text:
        raise CaptureError("payload must contain url or text/selected_text")

    entry_id = provisional_id(url)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rel = f"inbox/{entry_id}"

    if guard.exists(rel):
        return {"accepted": True, "entry_id": entry_id, "duplicate": True}

    capture_meta = {"url": url or None, "captured_at": now, "status": "inbox", "entry": entry}
    stored = dict(payload)
    stored["url"] = url or None
    stored["captured_at"] = now

    guard.write_json("capture", f"{rel}/capture.json", capture_meta)
    guard.write_json("capture", f"{rel}/payload.json", stored)
    return {"accepted": True, "entry_id": entry_id, "duplicate": False}
