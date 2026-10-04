"""库分区重置（§4.4 v0.44）：POST /api/kb/reset + 守卫 reset 通道回归。"""

from fastapi.testclient import TestClient

from kbserver.app import create_app
from kbserver.guard import WriteBoundaryError


def _seed(guard):
    """四区各放文件 + index.db 占位（重置不得删除）。"""
    kb = guard.kb_root
    (kb / "inbox" / "abc123").mkdir(parents=True)
    (kb / "inbox" / "abc123" / "capture.json").write_text("{}", encoding="utf-8")
    (kb / "sources" / "web" / "2026").mkdir(parents=True)
    (kb / "sources" / "web" / "2026" / "note.md").write_text("x", encoding="utf-8")
    (kb / "collections" / "coll1").mkdir(parents=True)
    (kb / "collections" / "coll1" / "collection.json").write_text("{}", encoding="utf-8")
    (kb / "wiki").mkdir(exist_ok=True)
    (kb / "wiki" / "w-1.md").write_text("x", encoding="utf-8")
    (kb / "index.db").write_text("sqlite", encoding="utf-8")


def test_reset_partial_deletes_only_selected(cfg, guard):
    """部分重置：选定区清空重建，其余区与 index.db 原样保留。"""
    _seed(guard)
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.post("/api/kb/reset", json={"regions": ["wiki", "inbox"], "confirm": "RESET"})
        assert r.status_code == 200
        body = r.json()
        assert sorted(body["reset"]) == ["inbox", "wiki"]
        assert body["counts"]["wiki"] == 1
        assert body["counts"]["inbox"] == 1
        assert body["backup"] is None
        # 选定区被清空重建
        assert (guard.kb_root / "wiki").is_dir() and not any((guard.kb_root / "wiki").iterdir())
        assert (guard.kb_root / "inbox").is_dir() and not any((guard.kb_root / "inbox").iterdir())
        # 其余区保留
        assert (guard.kb_root / "sources" / "web" / "2026" / "note.md").exists()
        assert (guard.kb_root / "collections" / "coll1" / "collection.json").exists()
        # index.db 属可重建缓存，端点不删（进程内被 SQLite 连接持有）
        assert (guard.kb_root / "index.db").exists()


def test_reset_collections_allowed(cfg, guard):
    """collections 可重置（v0.44 显式例外）：重置 = 废弃镜像含注册 collection.json。"""
    _seed(guard)
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.post("/api/kb/reset", json={"regions": ["collections"], "confirm": "RESET"})
        assert r.status_code == 200
        assert (guard.kb_root / "collections").is_dir()
        assert not any((guard.kb_root / "collections").iterdir())
        assert (guard.kb_root / "sources" / "web" / "2026" / "note.md").exists()


def test_reset_rejects_invalid_input(cfg, guard):
    """400 口径：非法区 / 空集 / 四区全选 / 确认文案不符。"""
    _seed(guard)
    app = create_app(cfg)
    with TestClient(app) as client:
        assert (
            client.post("/api/kb/reset", json={"regions": ["bogus"], "confirm": "RESET"}).status_code == 400
        )
        assert client.post("/api/kb/reset", json={"regions": [], "confirm": "RESET"}).status_code == 400
        assert (
            client.post(
                "/api/kb/reset",
                json={"regions": ["inbox", "sources", "collections", "wiki"], "confirm": "RESET"},
            ).status_code
            == 400
        )
        assert (
            client.post("/api/kb/reset", json={"regions": ["wiki"], "confirm": "yes"}).status_code == 400
        )
        # 误请求不产生任何删除
        assert (guard.kb_root / "wiki" / "w-1.md").exists()


def test_reset_with_backup(cfg, guard):
    """backup=true：先整库备份（排除 index.db）再重置。"""
    _seed(guard)
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.post(
            "/api/kb/reset", json={"regions": ["wiki"], "confirm": "RESET", "backup": True}
        )
        assert r.status_code == 200
        backup = r.json()["backup"]
        assert backup
        bdir = __import__("pathlib").Path(backup)
        assert bdir.is_dir()
        assert (bdir / "wiki" / "w-1.md").exists()
        assert not (bdir / "index.db").exists()
        assert not (guard.kb_root / "wiki" / "w-1.md").exists()


def test_guard_reset_stage_regions():
    """守卫面：reset 破坏性通道含四区（collections 显式例外）；curation 仍不含。"""
    import tempfile
    from pathlib import Path

    from kbserver.guard import Guard

    with tempfile.TemporaryDirectory() as td:
        g = Guard(Path(td))
        (g.kb_root / "collections" / "c1").mkdir(parents=True)
        (g.kb_root / "collections" / "c1" / "x.md").write_text("x", encoding="utf-8")
        g.remove_tree("reset", "collections")  # 允许（目录本身被删，重建是 reset_regions 职责）
        assert not (g.kb_root / "collections").exists()
        (g.kb_root / "collections" / "c2").mkdir(parents=True)
        try:
            g.remove_tree("curation", "collections")
            raise AssertionError("curation must not remove collections")
        except WriteBoundaryError:
            pass
