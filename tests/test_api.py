import json

from fastapi.testclient import TestClient

from kbserver.app import create_app

from .conftest import SAMPLE_HTML, make_fetcher


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
