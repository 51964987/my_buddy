import pytest

from kbserver.guard import WriteBoundaryError


def test_capture_cannot_write_sources(guard):
    with pytest.raises(WriteBoundaryError):
        guard.write_text("capture", "sources/web/2026/x/note.md", "nope")
    assert guard.violations["capture"] == 1


def test_normalize_cannot_write_wiki(guard):
    with pytest.raises(WriteBoundaryError):
        guard.write_text("normalize", "wiki/card-1.md", "nope")


def test_normalize_writes_sources_atomically(guard):
    rel = "sources/web/2026/abc123/note.md"
    guard.write_text("normalize", rel, "---\nstatus: normalized\n---\nbody")
    path = guard.kb_root.joinpath(*rel.split("/"))
    assert path.read_text(encoding="utf-8").endswith("body")
    leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_path_traversal_rejected(guard):
    with pytest.raises(WriteBoundaryError):
        guard.write_text("capture", "inbox/../sources/evil.md", "nope")
    with pytest.raises(WriteBoundaryError):
        guard.write_text("capture", "C:/Windows/evil.md", "nope")


def test_atomic_write_retries_transient_winerror5(guard, monkeypatch):
    # Windows 瞬时文件锁（杀软/索引器锁 tmp → os.replace WinError 5）：
    # 首次 replace 抛 PermissionError，重试后成功——原子写应有界重试而非直接失败
    import os as _os
    import time as _time

    real_replace = _os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "拒绝访问", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(_os, "replace", flaky_replace)
    monkeypatch.setattr("kbserver.guard.time.sleep", lambda s: None)  # 免测速等待

    rel = "sources/web/2026/retry/note.md"
    guard.write_text("normalize", rel, "内容")
    assert calls["n"] == 2  # 首次失败 + 重试成功
    assert guard.kb_root.joinpath(*rel.split("/")).read_text("utf-8") == "内容"


def test_unknown_stage_rejected(guard):
    with pytest.raises(WriteBoundaryError):
        guard.write_text("godmode", "sources/x.md", "nope")


def test_move_and_remove_tree(guard):
    guard.write_text("capture", "inbox/e1/payload.json", "{}")
    guard.move_tree("capture", "inbox/e1", "inbox/_archived/e1-t0")
    assert not guard.exists("inbox/e1/payload.json")
    assert guard.exists("inbox/_archived/e1-t0/payload.json")
    guard.remove_tree("capture", "inbox/_archived/e1-t0")
    assert not guard.exists("inbox/_archived/e1-t0")


def test_patch_note_fields_whitelist(guard):
    rel = "sources/web/2026/abc/note.md"
    guard.write_text(
        "normalize", rel,
        "---\nid: abc\nstatus: normalized\ntitle: keep me\ntags: []\n---\n\n正文保持不变\n",
    )

    # 白名单内字段可补丁更新
    fm = guard.patch_note_fields("enrich", rel, {"tags": ["a"]}, {"tags", "ai"})
    assert fm["tags"] == ["a"]

    # 越界字段拒绝（即使区域允许，字段不白名单也拦下）并计数
    with pytest.raises(WriteBoundaryError):
        guard.patch_note_fields("enrich", rel, {"title": "篡改"}, {"tags", "ai"})
    assert guard.violations["enrich"] == 1

    # 正文与其他字段原样保留
    text = guard.kb_root.joinpath(*rel.split("/")).read_text(encoding="utf-8")
    assert "title: keep me" in text
    assert "正文保持不变" in text

    # 不存在的文件拒绝
    with pytest.raises(WriteBoundaryError):
        guard.patch_note_fields("enrich", "sources/web/2026/none/note.md", {"tags": []}, {"tags", "ai"})
