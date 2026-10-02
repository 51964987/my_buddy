import json

import pytest

from kbserver.capture import CaptureError, accept_capture


def test_accept_url(guard):
    result = accept_capture(guard, {"url": "https://example.com/a?utm_source=x", "title": "t", "entry": "browser_ext"})
    assert result["accepted"] and not result["duplicate"]
    rel = f"inbox/{result['entry_id']}"
    capture = json.loads(guard.kb_root.joinpath(*f"{rel}/capture.json".split("/")).read_text("utf-8"))
    assert capture["status"] == "inbox"
    assert capture["entry"] == "browser_ext"
    assert capture["url"].startswith("https://example.com/a")
    payload = json.loads(guard.kb_root.joinpath(*f"{rel}/payload.json".split("/")).read_text("utf-8"))
    assert payload["title"] == "t"
    assert "captured_at" in payload


def test_accept_text_only(guard):
    result = accept_capture(guard, {"text": "随手记一条", "entry": "mobile_share"})
    assert result["accepted"]
    assert result["entry_id"].startswith("t")


def test_accept_idempotent(guard):
    payload = {"url": "https://example.com/dup", "entry": "cli"}
    first = accept_capture(guard, payload)
    second = accept_capture(guard, payload)
    assert first["entry_id"] == second["entry_id"]
    assert second["duplicate"] is True


def test_rejects_bad_payload(guard):
    with pytest.raises(CaptureError):
        accept_capture(guard, {"url": "ftp://example.com/x", "entry": "cli"})
    with pytest.raises(CaptureError):
        accept_capture(guard, {"url": "https://example.com/x", "entry": "hacker"})
    with pytest.raises(CaptureError):
        accept_capture(guard, {"entry": "cli"})
