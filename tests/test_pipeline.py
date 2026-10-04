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
    # SAMPLE_HTML 可见文本低于空壳阈值 → 旁路生成剥脚本快照（v0.43）
    assert meta["raw_files"] == ["raw/page.html", "raw/page.view.html"]
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


def test_screenshot_saved(cfg, guard):
    import base64

    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/shot": SAMPLE_HTML}))
    shot = base64.b64encode(b"\x89PNG-fake-screenshot").decode()
    r = _capture(guard, "https://example.com/shot", screenshot_b64=shot, entry="browser_ext")
    orch.scan_once()
    entry_dir = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    assert (entry_dir / "raw" / "screenshot.png").read_bytes() == b"\x89PNG-fake-screenshot"
    meta = json.loads((entry_dir / "meta.json").read_text("utf-8"))
    assert meta["raw_files"] == ["raw/page.html", "raw/page.view.html", "raw/screenshot.png"]
    assert meta["captured_from"] == "browser_ext"


def test_image_localization(cfg, guard):
    from kbserver.normalize import _localize_images

    fetcher = make_fetcher(images={"https://img.example.com/pic.png": b"\x89PNG-fake"})
    md = "![alt](https://img.example.com/pic.png)"
    raw_files = []
    out = _localize_images(md, "sources/web/2026/x", guard, fetcher, 30, raw_files)
    assert out.startswith("![alt](raw/img-")
    # v0.32：图片条目为 {path, src}（原站 URL 映射）
    assert raw_files and isinstance(raw_files[0], dict)
    assert raw_files[0]["src"] == "https://img.example.com/pic.png"
    assert raw_files[0]["path"].startswith("raw/img-")
    img_path = guard.kb_root.joinpath("sources/web/2026/x", *raw_files[0]["path"].split("/"))
    assert img_path.exists()


def test_absolutize_links():
    from kbserver.normalize import _absolutize_links

    md = "[rel](docs/page.md) and [abs](https://other.com/a) and [anchor](#sec)"
    out = _absolutize_links(md, "https://site.com/base/")
    assert "[rel](https://site.com/base/docs/page.md)" in out
    assert "[abs](https://other.com/a)" in out
    assert "[anchor](#sec)" in out


SPA_HTML = "<html><head><title>spa page</title></head><body></body></html>"


def test_extract_fallback_to_selection(cfg, guard):
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/spa": SPA_HTML}))
    r = _capture(guard, "https://example.com/spa", text="selected key content")
    orch.scan_once()

    entry_dir = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    note = (entry_dir / "note.md").read_text("utf-8")
    assert "selected key content" in note
    meta = json.loads((entry_dir / "meta.json").read_text("utf-8"))
    assert meta["extraction"] == "selection_fallback"
    # SPA 空壳仍保留；v0.43 起旁路生成剥脚本可浏览快照（兜底关闭时对原始件剥脚本）
    assert meta["raw_files"] == ["raw/page.html", "raw/page.view.html"]


def test_extract_error_without_selection(cfg, guard):
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/spa2": SPA_HTML}))
    r = _capture(guard, "https://example.com/spa2")
    for _ in range(3):
        orch.scan_once()
    data = _capture_json(guard, r["entry_id"])
    assert data["status"] == "error"
    assert data["error_stage"] == "extract"


def test_fetch_fail_fallback_then_full_recover(cfg, guard):
    fail = {"https://example.com/down"}
    pages = {}
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages=pages, fail_urls=fail))
    r = _capture(guard, "https://example.com/down", text="captured while offline")
    orch.scan_once()

    entry_dir = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    assert "captured while offline" in (entry_dir / "note.md").read_text("utf-8")
    meta = json.loads((entry_dir / "meta.json").read_text("utf-8"))
    assert meta["extraction"] == "selection_fallback"
    assert meta["raw_files"] == []

    fail.clear()
    pages["https://example.com/down"] = SAMPLE_HTML
    _capture(guard, "https://example.com/down")
    orch.scan_once()

    meta = json.loads((entry_dir / "meta.json").read_text("utf-8"))
    assert meta["extraction"] == "full"
    # 短正文页同样低于空壳阈值 → 生成剥脚本快照（兜底关闭，对原始件构造）
    assert meta["raw_files"] == ["raw/page.html", "raw/page.view.html"]
    assert "first paragraph" in (entry_dir / "note.md").read_text("utf-8")

RENDERED_HTML = """<html><head><title>rendered spa</title></head><body>
<article><h1>rendered spa</h1>
<p>dynamic rendered body content for playwright fallback.</p>
</article></body></html>"""


def _pw_fetcher(pages=None, fail=False):
    """可注入的假 Playwright 抓取器：记录调用，返回渲染后页面或抛错。"""
    calls = {"n": 0}

    def fetch(url: str, timeout: float = 15.0):
        calls["n"] += 1
        if fail:
            raise RuntimeError("playwright fetch failed: TimeoutError: goto")
        content = (pages or {}).get(url) or RENDERED_HTML
        from kbserver.normalize import FetchResult

        return FetchResult(
            url=url, final_url=url, content=content.encode("utf-8"),
            encoding="utf-8", status_code=200, content_type="text/html",
        )

    fetch.calls = calls
    return fetch


def test_playwright_fallback_recovers_fetch_failure(cfg, guard):
    """抓取失败 + 无选中文本 → Playwright 兜底渲染成功 → 完整落盘（meta.fetch.via=playwright）。"""
    cfg.data["normalize"]["playwright_fallback"] = True
    pw = _pw_fetcher()
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(fail_urls={"https://example.com/dyn"}), playwright_fetcher=pw)
    r = _capture(guard, "https://example.com/dyn")
    summary = orch.scan_once()
    assert summary["normalized"] == 1
    assert pw.calls["n"] == 1
    entry_dir = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    note = (entry_dir / "note.md").read_text("utf-8")
    assert "dynamic rendered body content" in note
    meta = json.loads((entry_dir / "meta.json").read_text("utf-8"))
    assert meta["fetch"]["via"] == "playwright"
    # v0.43：渲染态 DOM 旁路生成剥脚本快照（不再二次渲染），raw 原件为渲染后页面
    assert meta["raw_files"] == ["raw/page.html", "raw/page.view.html"]


def test_playwright_fallback_on_empty_extract(cfg, guard):
    """SPA 空壳提取为空 + 无选中文本 → Playwright 兜底重提取。"""
    cfg.data["normalize"]["playwright_fallback"] = True
    pw = _pw_fetcher()
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/spa3": SPA_HTML}), playwright_fetcher=pw)
    r = _capture(guard, "https://example.com/spa3")
    summary = orch.scan_once()
    assert summary["normalized"] == 1
    meta = json.loads(
        guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}/meta.json".split("/")).read_text("utf-8")
    )
    assert meta["fetch"]["via"] == "playwright"
    assert meta["extraction"] == "full"


def test_playwright_failure_stays_error_with_stage(cfg, guard):
    """兜底仍失败 → 按原失败阶段转 error，error_message 含 playwright 标记（可观测可重跑）。"""
    cfg.data["normalize"]["playwright_fallback"] = True
    pw = _pw_fetcher(fail=True)
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(fail_urls={"https://example.com/down2"}), playwright_fetcher=pw)
    r = _capture(guard, "https://example.com/down2")
    for _ in range(3):
        orch.scan_once()
    data = _capture_json(guard, r["entry_id"])
    assert data["status"] == "error"
    assert data["error_stage"] == "fetch"
    assert "playwright" in data["error_message"]


def test_playwright_not_attempted_with_selection(cfg, guard):
    """带选中文本走既有降级策略，不触发 Playwright（§11.4 归一化引擎职责卡口径）。"""
    cfg.data["normalize"]["playwright_fallback"] = True
    pw = _pw_fetcher()
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(fail_urls={"https://example.com/off"}), playwright_fetcher=pw)
    r = _capture(guard, "https://example.com/off", text="offline selection")
    orch.scan_once()
    assert pw.calls["n"] == 0
    entry_dir = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    meta = json.loads((entry_dir / "meta.json").read_text("utf-8"))
    assert meta["extraction"] == "selection_fallback"


def test_playwright_fallback_disabled_no_pw_call(cfg, guard):
    """开关关闭：兜底不触发，直连失败照旧转 fetch error。"""
    pw = _pw_fetcher()
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(fail_urls={"https://example.com/nopw"}), playwright_fetcher=pw)
    r = _capture(guard, "https://example.com/nopw")
    for _ in range(3):
        orch.scan_once()
    assert pw.calls["n"] == 0
    data = _capture_json(guard, r["entry_id"])
    assert data["status"] == "error"
    assert data["error_stage"] == "fetch"


# ---- 可浏览快照（§5.1 v0.43）----

LONG_HTML = (
    "<html><head><title>long static page</title></head><body><article>"
    + "<p>static article paragraph with plenty of readable text.</p>" * 6
    + "</article></body></html>"
)


def test_view_snapshot_stripped_from_original_when_fallback_off(cfg, guard):
    """空壳 + 兜底关闭：快照对原始件剥脚本，不渲染、raw/page.html 字节不变。"""
    pw = _pw_fetcher()
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/shell": SPA_HTML}), playwright_fetcher=pw)
    r = _capture(guard, "https://example.com/shell", text="selection text")
    orch.scan_once()
    assert pw.calls["n"] == 0
    d = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    raw = (d / "raw" / "page.html").read_text("utf-8")
    view = (d / "raw" / "page.view.html").read_text("utf-8")
    assert "<script" not in view.lower()
    assert '<base href="https://example.com/shell"' in view
    meta = json.loads((d / "meta.json").read_text("utf-8"))
    assert meta["raw_files"] == ["raw/page.html", "raw/page.view.html"]


def test_view_snapshot_renders_shell_with_pw(cfg, guard):
    """空壳 + 兜底开启：快照经 Playwright 渲染取真实 DOM（仅渲染这一次，正文策略不变）。"""
    cfg.data["normalize"]["playwright_fallback"] = True
    pw = _pw_fetcher()
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/spa9": SPA_HTML}), playwright_fetcher=pw)
    r = _capture(guard, "https://example.com/spa9", text="selection text")
    orch.scan_once()
    assert pw.calls["n"] == 1  # 仅快照渲染；带选中文本不触发正文兜底
    d = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    view = (d / "raw" / "page.view.html").read_text("utf-8")
    assert "dynamic rendered body content" in view
    assert "<script" not in view.lower()
    assert '<base href="https://example.com/spa9"' in view
    meta = json.loads((d / "meta.json").read_text("utf-8"))
    assert meta["extraction"] == "selection_fallback"  # 正文提取策略不受快照影响


def test_view_snapshot_pw_failure_degrades_to_original(cfg, guard):
    """快照渲染失败：降级对原始件剥脚本落盘，不影响条目本身转态。"""
    cfg.data["normalize"]["playwright_fallback"] = True
    pw = _pw_fetcher(fail=True)
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/spa10": SPA_HTML}), playwright_fetcher=pw)
    r = _capture(guard, "https://example.com/spa10", text="selection text")
    orch.scan_once()
    d = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    view = (d / "raw" / "page.view.html").read_text("utf-8")
    assert "<script" not in view.lower()
    meta = json.loads((d / "meta.json").read_text("utf-8"))
    assert meta["extraction"] == "selection_fallback"


def test_view_snapshot_skipped_for_static_full_page(cfg, guard):
    """静态正文页（可见文本充足）：page.html 本身可读，不生成快照。"""
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/long": LONG_HTML}))
    r = _capture(guard, "https://example.com/long")
    orch.scan_once()
    d = guard.kb_root.joinpath(*f"sources/web/2026/{r['entry_id']}".split("/"))
    meta = json.loads((d / "meta.json").read_text("utf-8"))
    assert meta["raw_files"] == ["raw/page.html"]
    assert not (d / "raw" / "page.view.html").exists()
