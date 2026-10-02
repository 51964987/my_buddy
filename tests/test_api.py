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


def test_index_rebuild_is_p4_stub(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        resp = client.post("/api/index/rebuild")
        assert resp.status_code == 503
        assert "P4" in resp.json()["detail"]
