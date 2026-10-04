"""P2c D 类同步引擎测试：注册/首抓/增量 diff/removed 保留/API 端点/写边界。"""

import pytest
from fastapi.testclient import TestClient

from kbserver.app import create_app
from kbserver.frontmatter import read_frontmatter
from kbserver.guard import WriteBoundaryError
from kbserver.idgen import collection_page_id
from kbserver.orchestrator import Orchestrator
from kbserver.sync import SitePage, SiteNode, SyncError, SyncEngine, build_page_paths

from .conftest import make_fetcher


class FakeParser:
    """假站点解析器：目录树与页面内容由测试注入。"""

    name = "fake"

    def __init__(self, nodes, pages):
        self.nodes = nodes
        self.pages = pages

    def fetch_toc(self, collection):
        return self.nodes

    def fetch_page(self, collection, node):
        page = self.pages.get(node.doc_id)
        if page is None:
            raise SyncError("fetch", f"no fixture page {node.doc_id}")
        return page


def make_payload(md: str, doc_id: int, code: str) -> SitePage:
    return SitePage(
        url=f"https://docs.example.com/docs/TestLib/doc/{code}",
        title=f"page-{code}",
        markdown=md,
        raw=b'{"fake": true}',
    )


def base_nodes():
    return [
        SiteNode(doc_id=1, code="Guide", parent_id=0, is_dir=True, title="指南"),
        SiteNode(doc_id=2, code="Intro", parent_id=1, is_dir=False, title="简介"),
        SiteNode(doc_id=3, code="Intro", parent_id=1, is_dir=False, title="简介二"),
    ]


def base_pages():
    return {
        2: make_payload("intro content v1", 2, "Intro"),
        3: make_payload("intro two content", 3, "Intro"),
    }


def test_volcengine_probe_url():
    # v0.22 连接向导：站点 URL → library_code；不匹配返回 None
    from kbserver.sync import VolcengineDocsParser

    probe = VolcengineDocsParser().probe_url
    assert probe("https://docs.volcengine.com/docs/bytehouse-x/doc/abc") == {"library_code": "bytehouse-x"}
    assert probe("https://docs.volcengine.com/docs/My.Lib_1/") == {"library_code": "My.Lib_1"}
    assert probe("https://www.volcengine.com/product/x") is None
    assert probe("https://docs.example.com/docs/x") is None
    assert probe("") is None


def register_payload():
    return {
        "id": "test-lib",
        "name": "测试文档站",
        "entry_url": "https://docs.example.com/docs/TestLib/list",
        "toc_parser": "fake",
        "library_code": "TestLib",
    }


def make_engine(cfg, guard, nodes=None, pages=None):
    parser = FakeParser(nodes if nodes is not None else base_nodes(), pages if pages is not None else base_pages())
    return SyncEngine(cfg, guard, parsers={parser.name: parser}), parser


# ---------- id 规则 ----------


def test_collection_page_id_deterministic():
    a = collection_page_id("test-lib", "/docs/TestLib/doc/Intro")
    b = collection_page_id("test-lib", "/docs/TestLib/doc/Intro")
    c = collection_page_id("other-lib", "/docs/TestLib/doc/Intro")
    assert a == b
    assert a != c
    assert len(a) == 12


# ---------- 路径清洗 ----------


def test_build_page_paths_collision_and_sanitize():
    nodes = [
        SiteNode(doc_id=1, code="Guide", parent_id=0, is_dir=True, title="d"),
        SiteNode(doc_id=2, code="Intro", parent_id=1, is_dir=False, title="p"),
        SiteNode(doc_id=3, code="Intro", parent_id=1, is_dir=False, title="p"),
        SiteNode(doc_id=4, code="a b/c", parent_id=0, is_dir=False, title="p"),
    ]
    paths = build_page_paths(nodes)
    assert paths[2] == "Guide/Intro"
    assert paths[3] == "Guide/Intro-3"  # 冲突编号化（5.3 第 6 条）
    assert paths[4] == "a-b-c"  # 非 ASCII 安全字符清洗
    assert all(len(p) <= 160 + 9 for p in paths.values())


def test_build_page_paths_overlong_gets_hash_suffix():
    # 多级目录叠加超长（单段已被 MAX_SEG_LEN 截断，只有链路叠加才会触发）
    nodes = []
    parent = 0
    for i in range(6):
        nodes.append(SiteNode(doc_id=i + 1, code=f"d{i}" * 15, parent_id=parent, is_dir=True, title="d"))
        parent = i + 1
    nodes.append(SiteNode(doc_id=99, code="p" * 30, parent_id=parent, is_dir=False, title="p"))
    paths = build_page_paths(nodes)
    assert len(paths[99]) <= 160 + 9
    assert "~" in paths[99]


# ---------- 注册校验 ----------


def test_register_validation(cfg, guard):
    engine, _ = make_engine(cfg, guard)
    with pytest.raises(SyncError):
        engine.register_collection({**register_payload(), "id": "Bad_ID"})
    with pytest.raises(SyncError):
        engine.register_collection({**register_payload(), "toc_parser": "nope"})
    with pytest.raises(SyncError):
        engine.register_collection({**register_payload(), "entry_url": "ftp://x"})
    coll = engine.register_collection(register_payload())
    assert coll["id"] == "test-lib"
    assert coll["sync"]["state"] == "registered"
    assert engine.get_collection("test-lib")["name"] == "测试文档站"


# ---------- 元数据更新（v0.50 PATCH 通道） ----------


def test_update_collection(cfg, guard):
    engine, _ = make_engine(cfg, guard)
    engine.register_collection(register_payload())

    # name-only：entry_url 不动
    coll = engine.update_collection("test-lib", {"name": "新名称"})
    assert coll["name"] == "新名称"
    assert coll["entry_url"] == "https://docs.example.com/docs/TestLib/list"

    # entry_url：重算 url_pattern（口径与注册一致），platform 同站不拒绝
    coll = engine.update_collection("test-lib", {"entry_url": "https://docs.example.com/docs/TestLib/index?lang=zh"})
    assert coll["entry_url"].endswith("index?lang=zh")
    assert coll["url_pattern"] == "docs.example.com/docs/TestLib/"

    # 同步状态字段不被破坏
    assert coll["sync"]["state"] == "registered"

    # 空 name 传入不覆盖既有名称（strip 后为空 = 忽略该字段）
    coll = engine.update_collection("test-lib", {"name": "", "entry_url": "https://docs.example.com/docs/TestLib/list"})
    assert coll["name"] == "新名称"

    # 校验：不存在 404 语义 / 空补丁 400 / 平台不一致 400（换平台 = 换 adapter 须重注册）
    with pytest.raises(SyncError):
        engine.update_collection("nope", {"name": "x"})
    with pytest.raises(SyncError):
        engine.update_collection("test-lib", {})
    with pytest.raises(SyncError):
        engine.update_collection("test-lib", {"entry_url": "https://docs.volcengine.com/docs/X/list"})


# ---------- 首抓与增量 ----------


def test_first_sync_and_layout(cfg, guard):
    engine, _ = make_engine(cfg, guard)
    engine.register_collection(register_payload())
    result = engine.sync("test-lib")

    assert result["pages"] == 2
    assert result["added"] == 2
    assert result["changed"] == 0
    assert result["removed"] == 0

    note_rel = "collections/test-lib/docs/Guide/Intro/note.md"
    note = guard.kb_root.joinpath(*note_rel.split("/"))
    assert note.exists()
    fm = read_frontmatter(note)
    assert fm["collection"] == "test-lib"
    assert fm["source_type"] == "product_doc"
    assert fm["status"] == "normalized"
    assert fm["id"] == collection_page_id("test-lib", "Guide/Intro")
    assert fm["url"] == "https://docs.example.com/docs/TestLib/doc/Intro"

    # toc.json 快照含 content_hash
    toc = guard.kb_root.joinpath("collections", "test-lib", "toc.json").read_text(encoding="utf-8")
    assert '"content_hash"' in toc

    # raw 原件保留（站点 API 响应体）
    assert (note.parent / "raw" / "page.json").exists()


def test_incremental_diff_changed_removed_duplicate(cfg, guard):
    engine, parser = make_engine(cfg, guard)
    engine.register_collection(register_payload())
    engine.sync("test-lib")

    # 页面 2 内容变更；页面 3 下架（removed）；其余不变
    parser.pages[2] = make_payload("intro content v2", 2, "Intro")
    parser.nodes = [n for n in parser.nodes if n.doc_id != 3]
    parser.pages.pop(3)

    result = engine.sync("test-lib")
    assert result["added"] == 0
    assert result["changed"] == 1
    assert result["removed"] == 1
    assert "Guide/Intro" in result["changed_paths"]
    assert "Guide/Intro-3" in result["removed_paths"]

    coll = engine.get_collection("test-lib")
    assert coll["sync"]["state"] == "ok"
    assert coll["sync"]["removed_pages"] == ["Guide/Intro-3"]

    # 变更页覆盖 note.md，旧 raw 归档（§5.3 第 4 条）
    note_dir = guard.kb_root / "collections" / "test-lib" / "docs" / "Guide" / "Intro"
    assert "v2" in (note_dir / "note.md").read_text(encoding="utf-8")
    archived = list((note_dir / "raw" / "archive").rglob("page.json"))
    assert len(archived) == 1

    # removed 页落盘文件保留（历史件人工裁决，不物理删除）
    removed_dir = guard.kb_root / "collections" / "test-lib" / "docs" / "Guide" / "Intro-3"
    assert removed_dir.exists()

    # 再跑一次无变化：不产生变更，不重复归档
    result2 = engine.sync("test-lib")
    assert result2["changed"] == 0
    assert result2["added"] == 0
    assert len(list((note_dir / "raw" / "archive").rglob("page.json"))) == 1


def test_url_drift_patched_without_renormalize(cfg, guard):
    # v0.27 回归：源站 URL 口径修正 → 已落盘页面 frontmatter url 走字段级补丁修复，
    # 不触发重新归一化（outcomes 全 0、status 不被重置）
    engine, parser = make_engine(cfg, guard)
    engine.register_collection(register_payload())
    engine.sync("test-lib")

    note_rel = "collections/test-lib/docs/Guide/Intro/note.md"
    before = (guard.kb_root.joinpath(*note_rel.split("/"))).read_text(encoding="utf-8")

    parser.pages[2] = SitePage(
        url="https://docs.example.com/docs/TestLib/Intro?lang=zh",  # URL 变化，内容不变
        title=parser.pages[2].title,
        markdown="intro content v1",
    )
    result = engine.sync("test-lib")
    assert result["changed"] == 0  # 内容未变，不重新归一化

    after = (guard.kb_root.joinpath(*note_rel.split("/"))).read_text(encoding="utf-8")
    assert "docs/TestLib/Intro?lang=zh" in after  # frontmatter url 已补丁
    assert "status: normalized" in after  # 状态未被重置


def test_page_fetch_error_recorded_not_silent(cfg, guard):
    engine, parser = make_engine(cfg, guard)
    engine.register_collection(register_payload())
    parser.pages.pop(3)  # 页面 3 抓取失败

    result = engine.sync("test-lib")
    assert result["pages"] == 1
    assert result["errors"] == 1
    coll = engine.get_collection("test-lib")
    assert coll["sync"]["errors"][0]["document_id"] == 3
    assert coll["sync"]["state"] == "ok"


def test_sync_unknown_collection(cfg, guard):
    engine, _ = make_engine(cfg, guard)
    with pytest.raises(SyncError):
        engine.sync("missing")


# ---------- API 端点 ----------


def test_collection_api_endpoints(cfg, guard):
    engine, _ = make_engine(cfg, guard)
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher())
    orch.sync_engine = engine  # 注入假解析器
    app = create_app(cfg, orchestrator=orch)
    with TestClient(app) as client:
        # 注册（worker 停用 → 同步执行首抓）
        resp = client.post("/api/collections", json=register_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["registered"] is True
        assert body["sync"]["mode"] == "inline"
        assert body["sync"]["result"]["pages"] == 2

        listing = client.get("/api/collections").json()
        assert listing["total"] == 1
        assert listing["collections"][0]["id"] == "test-lib"

        # 重复注册（再次下发同步 → 无变更：toc 哈希一致，不重复落盘）
        resp2 = client.post("/api/collections", json=register_payload())
        result2 = resp2.json()["sync"]["result"]
        assert result2["pages"] == 2
        assert result2["added"] == 0
        assert result2["changed"] == 0

        # 手动增量同步
        resp3 = client.post("/api/collections/test-lib/sync")
        assert resp3.status_code == 200

        # 未注册 collection 404
        assert client.post("/api/collections/missing/sync").status_code == 404

        # 校验失败 400
        bad = client.post("/api/collections", json={**register_payload(), "id": "!!"})
        assert bad.status_code == 400

        # status 带 collections 分区
        status = client.get("/api/status").json()
        assert status["collections"][0]["id"] == "test-lib"
        assert status["collections"][0]["pages"] == 2


# ---------- 写边界 ----------


def test_sync_stage_write_boundary(cfg, guard):
    with pytest.raises(WriteBoundaryError):
        guard.write_json("sync", "sources/evil.json", {"x": 1})
    with pytest.raises(WriteBoundaryError):
        guard.write_json("sync", "wiki/evil.json", {"x": 1})
    # sync 阶段只能写本 collections 区域
    guard.write_json("sync", "collections/c1/collection.json", {"ok": True})
