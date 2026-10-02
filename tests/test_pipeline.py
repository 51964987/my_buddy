import json

from kbserver.capture import accept_capture
from kbserver.orchestrator import Orchestrator

from .conftest import SAMPLE_HTML, make_fetcher


def _capture(guard, url, **kw):
    payload = {"url": url, "entry": "cli", **kw}
    return accept_capture(guard, payload)


def _note(guard, entry_rel):
    return guard.kb_root.joinpath(*f"{entry_rel}/note.md".split("/")).read_text("utf-8")


def _capture_json(guard, entry_id):
    p = guard.kb_root.joinpath(*f"inbox/{entry_id}/capture.json".split("/"))
    return json.loads(p.read_text("utf-8"))


def test_url_entry_normalized_e2e(cfg, guard):
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/a?utm_source=x": SAMPLE_HTML}))
    r = _capture(guard, "https://example.com/a?utm_source=x", title="my title")
    summary = orch.scan_once()
    assert summary["normalized"] == 1

    note = _note(guard, f"sources/web/2026/{r['entry_id']}")
    assert "status: normalized" in note
    assert f"id: {r['entry_id']}" in note
    assert "my title" in note  # payload title 优先于 <title>

    meta = json.loads(
        guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}/meta.json".split("/")).read_text("utf-8")
    )
    assert len(meta["content_hash"]) == 40
    assert meta["raw_files"] == ["raw/page.html"]
    assert meta["original_url"] == "https://example.com/a?utm_source=x"
    assert not guard.exists(f"inbox/{r['entry_id']}")


def test_duplicate_archived_not_deleted(cfg, guard):
    fetcher = make_fetcher(pages={"https://example.com/dup": SAMPLE_HTML})
    orch = Orchestrator(cfg, guard, fetcher=fetcher)
    r1 = _capture(guard, "https://example.com/dup")
    orch.scan_once()
    r2 = _capture(guard, "https://example.com/dup")
    assert r2["entry_id"] == r1["entry_id"]
    summary = orch.scan_once()
    assert summary["duplicate"] == 1
    notes = list((guard.kb_root / "sources").rglob("note.md"))
    assert len(notes) == 1
    archived = list((guard.kb_root / "inbox" / "_archived").iterdir())
    assert len(archived) == 1


def test_content_update_archives_old_raw(cfg, guard):
    pages = {"https://example.com/up": SAMPLE_HTML}
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages=pages))
    r = _capture(guard, "https://example.com/up")
    orch.scan_once()
    pages["https://example.com/up"] = SAMPLE_HTML.replace("first paragraph", "CHANGED paragraph")
    _capture(guard, "https://example.com/up")
    summary = orch.scan_once()
    assert summary["updated"] == 1

    entry_dir = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    meta = json.loads((entry_dir / "meta.json").read_text("utf-8"))
    note = (entry_dir / "note.md").read_text("utf-8")
    assert "CHANGED paragraph" in note
    archives = list((entry_dir / "raw" / "archive").iterdir())
    assert len(archives) == 1
    assert (archives[0] / "page.html").exists()
    assert (entry_dir / "raw" / "page.html").exists()


def test_error_state_and_rerun(cfg, guard):
    fail = {"https://example.com/err"}
    pages = {}
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages=pages, fail_urls=fail))
    r = _capture(guard, "https://example.com/err")

    orch.scan_once()
    orch.scan_once()
    assert _capture_json(guard, r["entry_id"])["status"] == "inbox"
    orch.scan_once()
    data = _capture_json(guard, r["entry_id"])
    assert data["status"] == "error"
    assert data["error_stage"] == "fetch"
    assert "boom" in data["error_message"]
    assert orch.status()["errors"], "error entry must be visible in status"

    assert orch.rerun(r["entry_id"]) is True
    data = _capture_json(guard, r["entry_id"])
    assert data["status"] == "inbox" and data["attempts"] == 0
    assert "error_stage" not in data

    fail.clear()
    pages["https://example.com/err"] = SAMPLE_HTML
    summary = orch.scan_once()
    assert summary["normalized"] == 1
    assert not guard.exists(f"inbox/{r['entry_id']}")


def test_text_note_e2e(cfg, guard):
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher())
    r = _capture(guard, None, text="随手记：一条想法\n第二行", title="随手记")
    orch.scan_once()
    note = _note(guard, f"sources/web/2026/{r['entry_id']}")
    assert "随手记：一条想法" in note
    assert "status: normalized" in note
    assert "url:" not in note.split("---")[1]


def test_image_localization(cfg, guard):
    from kbserver.normalize import _localize_images

    fetcher = make_fetcher(images={"https://img.example.com/pic.png": b"\x89PNG-fake"})
    md = "![alt](https://img.example.com/pic.png)"
    raw_files = []
    out = _localize_images(md, "sources/web/2026/x", guard, fetcher, 30, raw_files)
    assert out.startswith("![alt](raw/img-")
    assert raw_files and raw_files[0].startswith("raw/img-")
    img_path = guard.kb_root.joinpath("sources/web/2026/x", *raw_files[0].split("/"))
    assert img_path.exists()


def test_absolutize_links():
    from kbserver.normalize import _absolutize_links

    md = "[rel](docs/page.md) and [abs](https://other.com/a) and [anchor](#sec)"
    out = _absolutize_links(md, "https://site.com/base/")
    assert "[rel](https://site.com/base/docs/page.md)" in out
    assert "[abs](https://other.com/a)" in out
    assert "[anchor](#sec)" in out
