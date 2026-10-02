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
