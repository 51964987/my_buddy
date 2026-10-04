import json

from fastapi.testclient import TestClient

from kbserver.app import create_app

from .conftest import SAMPLE_HTML, make_fetcher


def test_wiki_edit_api_status_codes(cfg, guard):
    """PUT /api/wiki/{card_id}：200 修订成功 / 404 卡不存在 / 409 非 draft / 400 空正文（v0.35）。"""
    from kbserver import curation

    curation_dir = guard.kb_root / "wiki"
    curation_dir.mkdir(parents=True, exist_ok=True)
    (curation_dir / "w-edit1.md").write_text(
        "---\nid: w-edit1\ntype: summary\nai_generated: true\nstatus: draft\nsources: [seed1]\n---\n\n# 标题\n\n原正文。\n",
        encoding="utf-8",
    )
    (curation_dir / "w-edit2.md").write_text(
        "---\nid: w-edit2\ntype: summary\nai_generated: true\nstatus: promoted\nsources: [seed1]\n---\n\n# 标题\n\n已晋升。\n",
        encoding="utf-8",
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        ok = client.put("/api/wiki/w-edit1", json={"body": "# 标题\n\n人工修订正文。"})
        assert ok.status_code == 200
        assert ok.json()["edited_at"]
        detail = client.get("/api/wiki/w-edit1").json()
        assert "人工修订正文" in detail["body"]
        assert detail["status"] == "draft"
        assert detail["edited_at"]

        assert client.put("/api/wiki/w-nope", json={"body": "x"}).status_code == 404
        assert client.put("/api/wiki/w-edit2", json={"body": "x"}).status_code == 409
        assert client.put("/api/wiki/w-edit1", json={"body": "  "}).status_code == 400


def test_capture_and_status_api(cfg, guard):
    cfg.data["pipeline"]["worker_enabled"] = False
    app = create_app(cfg)
    with TestClient(app) as client:
        resp = client.post("/api/capture", json={"url": "https://example.com/api1", "title": "t", "entry": "browser_ext"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True

        status = client.get("/api/status").json()
        assert status["inbox"]["inbox"] == 1
        assert status["version"]

        entries = client.get("/api/entries").json()
        assert entries == {"entries": [], "total": 0}

        bad = client.post("/api/capture", json={"entry": "cli"})
        assert bad.status_code == 400


def test_entries_api_after_pipeline(cfg, guard):
    from kbserver.orchestrator import Orchestrator

    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/api2": SAMPLE_HTML}))
    app = create_app(cfg)
    with TestClient(app) as client:
        client.post("/api/capture", json={"url": "https://example.com/api2", "entry": "cli"})
        orch.scan_once()

        listing = client.get("/api/entries").json()
        assert listing["total"] == 1
        entry = listing["entries"][0]
        assert entry["status"] == "normalized"
        assert entry["platform"] == "web"

        detail = client.get(f"/api/entries/{entry['id']}").json()
        assert detail["frontmatter"]["id"] == entry["id"]
        assert "first paragraph" in detail["body"]
        assert detail["meta"]["raw_files"] == ["raw/page.html"]
        assert "raw/page.html" in detail["raw_files"]

        assert client.get("/api/entries/missing").status_code == 404


def test_delete_entry_api(cfg, guard):
    # 条目删除处置（§4.4 v0.20）：物理删除 sources 条目目录（含 raw/），404 兜底；
    # 删除后 /api/entries 立即少一条（索引由懒同步自动移除，此处不重复验证）
    from kbserver.orchestrator import Orchestrator

    orch = Orchestrator(cfg, guard, fetcher=make_fetcher(pages={"https://example.com/del1": SAMPLE_HTML}))
    app = create_app(cfg)
    with TestClient(app) as client:
        client.post("/api/capture", json={"url": "https://example.com/del1", "entry": "cli"})
        orch.scan_once()
        entry = client.get("/api/entries").json()["entries"][0]

        resp = client.delete(f"/api/entries/{entry['id']}")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True, "entry_id": entry["id"]}
        assert client.get("/api/entries").json()["total"] == 0
        assert client.get(f"/api/entries/{entry['id']}").status_code == 404

        # 已删除后再删 → 404
        assert client.delete(f"/api/entries/{entry['id']}").status_code == 404


def test_delete_entry_rejects_collection_page(cfg, guard, tmp_path):
    # D 类 collection 页面为站点镜像（选项 A 拍板）：不进 entries 语料（只扫 sources/），
    # 删除请求 404；守卫区域白名单（curation 不含 collections）确保即使误入也不可删
    docs = tmp_path / "kb" / "collections" / "c1" / "docs"
    (docs / "pageA").mkdir(parents=True)
    (docs / "pageA" / "note.md").write_text(
        "---\nid: collpg00001\ntitle: p\nurl: https://x/1\nplatform: web\nsource_type: docs\n"
        "status: normalized\ncollection_id: c1\ncollection_page: true\ncaptured_at: \"2026-10-03T00:00:00\"\n---\n\n正文",
        encoding="utf-8",
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/api/entries").json()["total"] == 0  # collection 页不进条目列表

        resp = client.delete("/api/entries/collpg00001")
        assert resp.status_code == 404
        assert (docs / "pageA" / "note.md").exists()  # 文件未被删除


def test_delete_inbox_entry_api(cfg, guard):
    # inbox 丢弃通道（§4.4 v0.21）：物理删除 inbox/<id>/ 整目录；404 兜底；非法 id 拒绝
    app = create_app(cfg)
    with TestClient(app) as client:
        resp = client.post("/api/capture", json={"url": "https://example.com/inbox-del", "entry": "cli"})
        assert resp.status_code == 200
        inbox_id = resp.json()["entry_id"]
        assert guard.exists(f"inbox/{inbox_id}/capture.json")

        resp = client.delete(f"/api/inbox/{inbox_id}")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True, "entry_id": inbox_id}
        assert not guard.exists(f"inbox/{inbox_id}")
        assert client.delete(f"/api/inbox/{inbox_id}").status_code == 404

        # 非法 id（路径注入字符）拒绝：含 ".." 的 400（端点白名单），含 %2F 的路由层 404，均不触碰文件
        assert client.delete("/api/inbox/a..b").status_code == 400
        assert client.delete("/api/inbox/x%2Fy").status_code in (400, 404)


def test_collection_toc_tri_state(cfg, guard):
    # v0.23：toc 端点三态——未注册 404 / 已注册但首抓未完成（toc.json 缺失）409 / 正常 200
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/api/collections/none/toc").status_code == 404

        coll = {"id": "c9", "toc_parser": "volcengine", "site": {"library_code": "X", "lang": "zh"}}
        guard.write_json("sync", "collections/c9/collection.json", coll)
        resp = client.get("/api/collections/c9/toc")
        assert resp.status_code == 409
        assert "首抓" in resp.json()["detail"]


def test_collection_page_detail_via_entry_id(cfg, guard):
    # v0.26 回归：D 类 collection 页面详情按 id 可查（文档树 openPage 依赖）；
    # 时间流清单口径不变（仍只列 sources/，collection 页不进列表）
    page_dir = guard.kb_root / "collections" / "c1" / "docs" / "Productoverview"
    page_dir.mkdir(parents=True)
    (page_dir / "note.md").write_text(
        "---\nid: collpg00002\ntitle: 产品简介\nurl: https://x/1\nplatform: volcengine\nsource_type: product_doc\n"
        "status: normalized\ncollection_id: c1\ncollection_page: true\ncaptured_at: \"2026-10-03T00:00:00\"\n---\n\n正文内容",
        encoding="utf-8",
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        detail = client.get("/api/entries/collpg00002")
        assert detail.status_code == 200
        assert detail.json()["frontmatter"]["title"] == "产品简介"
        assert "正文内容" in detail.json()["body"]

        listing = client.get("/api/entries").json()
        assert listing["total"] == 0  # 清单口径不变：collection 页不进时间流


def test_token_auth_and_config_masking(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        client.put("/api/config", json={"config": {"token": "s3cret", "port": 9999}})
        assert cfg.data["token"] == "s3cret"
        assert (
            client.put("/api/config", json={"config": {"port": 9999}}, headers={"X-KB-Token": "s3cret"}).json()[
                "restart_required"
            ]
            is False
        )

        # token 热加载生效：未带 token 的请求立即被拒
        assert client.get("/api/config").status_code == 401

        masked = client.get("/api/config", headers={"X-KB-Token": "s3cret"}).json()["config"]
        assert masked["token"] == "******"
        assert masked["port"] == 9999

        # 回传掩码值 → 保持原值
        client.put("/api/config", json={"config": {"token": "******"}}, headers={"X-KB-Token": "s3cret"})
        assert cfg.data["token"] == "s3cret"

        assert (
            client.put("/api/config", json={"config": {"host": "0.0.0.0"}}, headers={"X-KB-Token": "s3cret"}).json()[
                "restart_required"
            ]
            is True
        )


def test_token_enforced_when_configured_at_startup(cfg):
    cfg.data["token"] = "topsecret"
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"X-KB-Token": "topsecret"}).status_code == 200


def test_search_and_index_rebuild_api(cfg, guard):
    from .test_indexer import make_note

    make_note(guard, "sources/a/note.md", title="SQLite 全文检索笔记", body="FTS5 建表要显式配置中文分词。", status="normalized")
    app = create_app(cfg)
    with TestClient(app) as client:
        # 首次检索触发懒同步（含建库）
        resp = client.get("/api/search", params={"q": "分词"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["results"][0]["kind"] == "entry"
        assert "分词" in data["results"][0]["snippet"]

        # 重建指令：全量重扫
        resp = client.post("/api/index/rebuild")
        assert resp.status_code == 200
        assert resp.json()["rebuilt"] is True
        assert resp.json()["docs"] == 1

        # 空查询 / 纯符号查询 → 空结果不报错（不拼 MATCH）
        for bad_q in ("", "!!!"):
            resp = client.get("/api/search", params={"q": bad_q})
            assert resp.status_code == 200
            assert resp.json()["total"] == 0


def test_enrich_run_and_rerun_api(cfg, guard):
    from kbserver.enrich import Enricher
    from kbserver.orchestrator import Orchestrator

    class FakeLLM:
        def __call__(self, task):
            replies = {
                "tags": '["api"]',
                "summary_card": '{"summary": "摘要", "card": "## 要点"}',
                "entity_extraction": '{"entities": [], "relations": []}',
            }

            class _C:
                def chat(self, messages, temperature=0.2):
                    return replies[task]

            return _C()

    orch = Orchestrator(
        cfg,
        guard,
        fetcher=make_fetcher(pages={"https://example.com/api3": SAMPLE_HTML}),
        enricher=Enricher(cfg, guard, client_factory=FakeLLM()),
    )
    app = create_app(cfg, orchestrator=orch)
    with TestClient(app) as client:
        # ai.enabled 默认 False → 409 明确拒绝
        resp = client.post("/api/enrich/run")
        assert resp.status_code == 409

        client.post("/api/capture", json={"url": "https://example.com/api3", "entry": "cli"})
        orch.scan_once()
        entry_id = client.get("/api/entries").json()["entries"][0]["id"]

        cfg.data["ai"]["enabled"] = True
        resp = client.post("/api/enrich/run", json=None, params={"entry_id": entry_id})
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "enriched"

        status = client.get("/api/status").json()
        assert status["enrich"]["enriched"] == 1
        assert status["ai_enabled"] is True

        # 非 error 态条目 rerun：inbox 与 enrich 两条路都找不到 → 404
        assert client.post(f"/api/entries/{entry_id}/rerun").status_code == 404

# ---------------- 工作台与人工处置 API（P5，§6 v0.17） ----------------


def _seed_note(guard, entry_id="seed0001", status="enriched"):
    import yaml

    rel = f"sources/web/2026/{entry_id}/note.md"
    fm = {"id": entry_id, "title": f"t-{entry_id}", "status": status, "tags": []}
    guard.write_text(
        "normalize", rel, "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n正文。\n"
    )
    guard.write_json("enrich", f"sources/web/2026/{entry_id}/meta.json", {"enrich": {"attempts": 2}})
    return rel


def _make_card(guard, card_id, sources, status="draft"):
    import yaml

    fm = {
        "id": card_id, "type": "summary", "ai_generated": True, "status": status,
        "sources": sources, "confidence": 0.9, "created_at": "2026-10-03T10:00:00+08:00", "model": "m",
    }
    guard.write_text(
        "enrich", f"wiki/{card_id}.md",
        "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n# 卡标题\n\n要点。\n",
    )


def test_wiki_curation_api_flow(cfg, guard):
    _seed_note(guard, "seed0001")
    _make_card(guard, "w-a1", ["seed0001"])
    app = create_app(cfg)
    with TestClient(app) as client:
        cards = client.get("/api/wiki").json()
        assert cards["total"] == 1
        assert cards["cards"][0]["ai_generated"] is True

        detail = client.get("/api/wiki/w-a1").json()
        assert detail["title"] == "卡标题"
        assert client.get("/api/wiki/w-nope").status_code == 404

        # 晋升
        assert client.post("/api/wiki/w-a1/promote").json() == {"promoted": True, "card_id": "w-a1"}
        assert client.post("/api/wiki/w-a1/promote").status_code == 409  # 非 draft 拒绝

        # 打回重生成：卡删除 + 源条目复位
        result = client.post("/api/wiki/w-a1/regenerate").json()
        assert result["reset_sources"] == ["seed0001"]
        assert client.get("/api/wiki/w-a1").status_code == 404
        note = guard.kb_root.joinpath(*"sources/web/2026/seed0001/note.md".split("/")).read_text("utf-8")
        assert "status: normalized" in note

        # 删除（含 404）
        _make_card(guard, "w-b2", ["ghost"])
        assert client.post("/api/wiki/w-b2/delete").json()["deleted"] is True
        assert client.post("/api/wiki/w-b2/delete").status_code == 404


def test_enrich_logs_and_facets_and_parsers_api(cfg, guard):
    import json as _json

    _seed_note(guard, "seed0001")
    meta_rel = "sources/web/2026/seed0001/meta.json"
    meta = _json.loads(guard.kb_root.joinpath(*meta_rel.split("/")).read_text("utf-8"))
    meta["enrich"]["log"] = [{"at": "2026-10-03T10:00:00+08:00", "outcome": "enriched", "attempts": 1}]
    guard.write_json("enrich", meta_rel, meta)

    app = create_app(cfg)
    with TestClient(app) as client:
        logs = client.get("/api/enrich/logs").json()["logs"]
        assert len(logs) == 1
        assert logs[0]["entry_id"] == "seed0001"

        facets = client.get("/api/facets").json()
        assert "web" in facets["platforms"]  # 注册表值
        assert "product_doc" in facets["source_types"]

        parsers = client.get("/api/parsers").json()["parsers"]
        assert {p["name"] for p in parsers} == {"volcengine"}
        # v0.22：解析器元数据带字段级提示（name/hint/placeholder），注册表单据此动态渲染（前端不硬编码）
        assert parsers[0]["fields"][0]["name"] == "library_code"
        assert "docs" in parsers[0]["fields"][0]["hint"]

        # v0.22 站点探测：URL 匹配 adapter + 提取参数（verify=False 不发网络请求）
        probe = client.post("/api/parsers/probe", json={"url": "https://docs.volcengine.com/docs/bytehouse-x/doc/a", "verify": False}).json()
        assert probe["matched"] is True and probe["parser"] == "volcengine"
        assert probe["params"] == {"library_code": "bytehouse-x"}
        miss = client.post("/api/parsers/probe", json={"url": "https://docs.example.com/guide/x", "verify": False}).json()
        assert miss["matched"] is False and miss["parser"] is None
        assert client.post("/api/parsers/probe", json={"url": ""}).status_code == 400

        # toc：未注册 collection → 404
        assert client.get("/api/collections/none/toc").status_code == 404

        # 语义检索未启用 → 409（明确提示，不静默回落）
        resp = client.get("/api/search", params={"q": "x", "mode": "semantic"})
        assert resp.status_code == 409
        assert "disabled" in resp.json()["detail"]

        st = client.get("/api/status").json()
        assert st["index"]["vector"]["enabled"] is False
        assert st["index"]["docs"] == 0


def test_webui_static_served(cfg):
    """P5：webui 构建产物经 /app 托管；SPA 前端路由路径回落 index.html。

    回归：StaticFiles 抛 starlette.HTTPException（fastapi.HTTPException 的基类），
    回退必须 except 基类——曾有 except fastapi.HTTPException 匹配不到导致
    /app/settings 404 的踩坑（v0.17）。
    """
    app = create_app(cfg)
    with TestClient(app) as client:
        resp = client.get("/app/")
        assert resp.status_code == 200
        assert "kb buddy" in resp.text

        spa = client.get("/app/settings")
        assert spa.status_code == 200
        assert "kb buddy" in spa.text  # 前端路由路径回落 index.html
