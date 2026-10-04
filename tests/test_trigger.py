"""v0.29 AI 整理触发门控 / 试跑（dry-run）/ 熔断测试（§5.1 enrich 触发门控口径）。

覆盖：
- `ai.enabled` 与"谁来触发"解耦：默认 manual 不自动扫库，手动指令仍可用
- auto 模式按 batch_size 自动扫库；总闸关时自动路径不动
- 熔断：整轮全失败累计 → open → 静默停自动扫描；手动不受门控；显式 reset 恢复
- 试跑零落盘（不写 wiki/、不改 frontmatter、不计 attempts、不写流水），预览结果 = 实际落盘
- trigger_mode 枚举校验；poll_interval 每轮实时读
"""

import pytest
from fastapi.testclient import TestClient

from kbserver.app import create_app
from kbserver.capture import accept_capture
from kbserver.enrich import Enricher, EntryNotFound, EntryStateError, wiki_card_id
from kbserver.frontmatter import split_note
from kbserver.orchestrator import Orchestrator

from .conftest import SAMPLE_HTML, make_fetcher
from .test_enrich import CARD_REPLY, TAGS_REPLY, FakeLLM

U1 = "https://example.com/t1"
U2 = "https://example.com/t2"


def _orch(cfg, guard, llm, urls=(U1, U2)):
    enricher = Enricher(cfg, guard, client_factory=llm)
    fetcher = make_fetcher(pages={u: SAMPLE_HTML for u in urls})
    return Orchestrator(cfg, guard, fetcher=fetcher, enricher=enricher)


def _capture(orch, guard, url=U1):
    r = accept_capture(guard, {"url": url, "entry": "cli"})
    orch.scan_once()
    notes = list((guard.kb_root / "sources").rglob("note.md"))
    return r, notes[0]


def _fm(note_path):
    return split_note(note_path.read_text(encoding="utf-8"))[0]


def _wiki(guard):
    wiki = guard.kb_root / "wiki"
    return list(wiki.glob("*.md")) if wiki.exists() else []


# ---------- 门控：manual（默认）不自动跑，手动仍可用 ----------


def test_default_trigger_mode_is_manual(cfg):
    # 防回归：默认必须是 manual——"开了 ai.enabled"不等于"自动全量跑"
    assert cfg.data["ai"]["trigger_mode"] == "manual"


def test_manual_mode_blocks_auto_worker_but_manual_run_works(cfg, guard):
    cfg.data["ai"]["enabled"] = True  # 只开总闸
    llm = FakeLLM()
    orch = _orch(cfg, guard, llm, urls=(U1,))
    r, note = _capture(orch, guard)

    skipped = orch.enrich_auto_once()
    assert skipped["skipped_reason"] == "manual_mode"
    assert skipped["scanned"] == 0
    assert _fm(note)["status"] == "normalized"  # 未被自动加工
    assert _wiki(guard) == []
    assert llm.prompts == []  # 一个 LLM 调用都没发生

    # 手动跑一批（人触发不受 trigger_mode 门控）
    out = orch.enrich_run()
    assert out["batch"]["enriched"] == 1
    assert _fm(note)["status"] == "enriched"
    assert _wiki(guard)


def test_auto_mode_dispatches_one_batch(cfg, guard):
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["trigger_mode"] = "auto"
    cfg.data["ai"]["batch_size"] = 2
    orch = _orch(cfg, guard, FakeLLM())
    _capture(orch, guard, U1)
    _capture(orch, guard, U2)

    summary = orch.enrich_auto_once()
    assert summary["skipped_reason"] is None
    assert summary["scanned"] == 2
    assert summary["enriched"] == 2
    assert [r["outcome"] for r in summary["results"]] == ["enriched", "enriched"]
    assert orch.auto_dispatch_allowed() is True


def test_auto_path_noop_when_master_switch_off(cfg, guard):
    cfg.data["ai"]["enabled"] = False
    cfg.data["ai"]["trigger_mode"] = "auto"
    llm = FakeLLM()
    orch = _orch(cfg, guard, llm, urls=(U1,))
    _capture(orch, guard)

    summary = orch.enrich_auto_once()
    assert summary["skipped_reason"] == "ai_disabled"
    assert llm.prompts == []


# ---------- 熔断 ----------


def test_breaker_opens_blocks_auto_but_not_manual(cfg, guard):
    cfg.data["ai"].update(
        {"enabled": True, "trigger_mode": "auto", "batch_size": 1, "breaker_threshold": 1}
    )
    llm = FakeLLM()
    llm.fail_tasks = {"tags"}
    orch = _orch(cfg, guard, llm, urls=(U1,))
    _capture(orch, guard)

    first = orch.enrich_auto_once()
    # 首轮未超 max_attempts → outcome 为 retry（但仍算"整轮无成功"，熔断照常累计）
    assert first["retry"] == 1
    breaker = cfg.data["ai"]["breaker"]
    assert breaker["state"] == "open"
    assert breaker["consecutive_failures"] == 1
    assert breaker["opened_at"]
    assert "boom" in breaker["last_error"]

    # 熔断后自动路径静默停下（仍 processed 条目未超重试上限，normalized 状态不变）
    second = orch.enrich_auto_once()
    assert second["skipped_reason"] == "breaker_open"
    assert second["scanned"] == 0
    assert orch.auto_dispatch_allowed() is False

    # 手动路径不受熔断门控：人仍可发起
    manual = orch.enrich_run()
    assert manual["batch"]["scanned"] == 1


def test_breaker_reset_is_the_only_recovery(cfg, guard):
    cfg.data["ai"].update(
        {"enabled": True, "trigger_mode": "auto", "batch_size": 1, "breaker_threshold": 1}
    )
    llm = FakeLLM()
    llm.fail_tasks = {"tags"}
    orch = _orch(cfg, guard, llm, urls=(U1,))
    _capture(orch, guard)
    orch.enrich_auto_once()
    assert cfg.data["ai"]["breaker"]["state"] == "open"

    state = orch.breaker_reset()
    assert state["state"] == "closed"
    assert state["consecutive_failures"] == 0
    assert state["opened_at"] is None
    assert state["last_error"] is None
    # 恢复后自动路径重新工作（模型仍失败 → 再次累计并重新 open）
    again = orch.enrich_auto_once()
    assert again["skipped_reason"] is None
    assert again["retry"] == 1
    assert cfg.data["ai"]["breaker"]["state"] == "open"


def test_breaker_counter_resets_on_success_round(cfg, guard):
    cfg.data["ai"].update(
        {"enabled": True, "trigger_mode": "auto", "batch_size": 1, "breaker_threshold": 3}
    )
    llm = FakeLLM()
    llm.fail_tasks = {"tags"}
    orch = _orch(cfg, guard, llm, urls=(U1,))
    _capture(orch, guard)

    assert orch.enrich_auto_once()["retry"] == 1
    assert cfg.data["ai"]["breaker"]["consecutive_failures"] == 1

    llm.fail_tasks = set()  # 模型恢复
    assert orch.enrich_auto_once()["enriched"] == 1
    assert cfg.data["ai"]["breaker"]["consecutive_failures"] == 0
    assert cfg.data["ai"]["breaker"]["state"] == "closed"


def test_breaker_ignores_round_without_targets(cfg, guard):
    cfg.data["ai"].update({"enabled": True, "trigger_mode": "auto"})
    orch = _orch(cfg, guard, FakeLLM())
    summary = orch.enrich_auto_once()  # 库为空，无 normalized 目标
    assert summary["scanned"] == 0
    assert cfg.data["ai"]["breaker"]["consecutive_failures"] == 0
    assert cfg.data["ai"]["breaker"]["state"] == "closed"


def test_breaker_state_persisted_to_config_file(cfg, guard):
    cfg.data["ai"].update({"enabled": True, "trigger_mode": "auto", "breaker_threshold": 1})
    llm = FakeLLM()
    llm.fail_tasks = {"tags"}
    orch = _orch(cfg, guard, llm, urls=(U1,))
    _capture(orch, guard)
    orch.enrich_auto_once()

    saved = cfg.masked()
    assert saved["ai"]["breaker"]["state"] == "open"
    # 落盘可读回（程序写运行态经配置中心原子写，配置文件不属 kb/ 不经写边界守卫）
    assert cfg.path.exists()


# ---------- 试跑（dry-run，零落盘） ----------


def test_preview_has_no_side_effects_and_matches_execution(cfg, guard):
    cfg.data["ai"]["enabled"] = True
    llm = FakeLLM()
    orch = _orch(cfg, guard, llm, urls=(U1,))
    r, note = _capture(orch, guard)
    meta_path = note.parent / "meta.json"
    meta_before = meta_path.read_text(encoding="utf-8")

    plan = orch.enrich_preview(r["entry_id"])
    assert plan["tags"] == ["python", "知识库"]
    assert "## 要点" in plan["card_body"]
    assert plan["card_id"] == wiki_card_id(r["entry_id"])
    assert plan["provider"] == "ollama"
    assert plan["model"] == "qwen2.5:1.5b"  # provider 默认 model
    assert plan["base_url"].startswith("http://127.0.0.1:11434")
    assert plan["elapsed_ms"] >= 0

    # 零落盘：wiki 空、frontmatter 未变、meta.json 未记 attempts/log
    assert _wiki(guard) == []
    fm = _fm(note)
    assert fm["status"] == "normalized"
    assert not fm.get("ai")
    assert not fm.get("tags")
    assert meta_path.read_text(encoding="utf-8") == meta_before

    # 预览 = 实际执行：同一份 prompt/解析，落盘结果与预览一致
    assert orch.enrich_run(r["entry_id"])["outcome"] == "enriched"
    assert _fm(note)["tags"] == plan["tags"]
    cards = _wiki(guard)
    summary_cards = [p for p in cards if "type: summary" in p.read_text(encoding="utf-8")]
    assert len(summary_cards) == 1  # v0.40 起另有实体卡，按 type 过滤摘要卡
    assert plan["card_body"] in summary_cards[0].read_text(encoding="utf-8")


def test_preview_errors_for_unknown_and_enriched_entry(cfg, guard):
    cfg.data["ai"]["enabled"] = True
    orch = _orch(cfg, guard, FakeLLM(), urls=(U1,))
    r, _note = _capture(orch, guard)

    with pytest.raises(EntryNotFound):
        orch.enrich_preview("does-not-exist")

    assert orch.enrich_run(r["entry_id"])["outcome"] == "enriched"
    with pytest.raises(EntryStateError):
        orch.enrich_preview(r["entry_id"])  # 状态已是 enriched，无可整理内容


# ---------- 手动批量 outcome 汇总 ----------


def test_manual_batch_returns_per_entry_results(cfg, guard):
    cfg.data["ai"].update({"enabled": True, "batch_size": 5})
    llm = FakeLLM()
    orch = _orch(cfg, guard, llm)
    _capture(orch, guard, U1)
    _capture(orch, guard, U2)

    out = orch.enrich_run()
    results = out["batch"]["results"]
    assert len(results) == 2
    assert all(r["outcome"] == "enriched" and r["title"] and r["entry_id"] for r in results)

    # 第二轮：无 pending 目标（都 enriched）
    assert out["batch"]["scanned"] == 2
    assert orch.enrich_run()["batch"]["scanned"] == 0


def test_manual_batch_reports_failure_message_per_entry(cfg, guard):
    cfg.data["ai"].update({"enabled": True, "batch_size": 1})
    llm = FakeLLM()
    llm.fail_tasks = {"summary_card"}
    orch = _orch(cfg, guard, llm, urls=(U1,))
    _capture(orch, guard)

    results = orch.enrich_run()["batch"]["results"]
    assert results[0]["outcome"] in ("retry", "error")
    assert "boom" in results[0]["message"]


# ---------- 配置：枚举校验与实时读取 ----------


def test_invalid_trigger_mode_rejected_before_merge(cfg):
    with pytest.raises(ValueError):
        cfg.apply_update({"ai": {"trigger_mode": "Auto"}})
    assert cfg.data["ai"]["trigger_mode"] == "manual"  # 非法值不留脏状态
    cfg.apply_update({"ai": {"trigger_mode": "auto"}})
    assert cfg.data["ai"]["trigger_mode"] == "auto"


def test_poll_interval_read_each_round(cfg, guard):
    orch = _orch(cfg, guard, FakeLLM())
    cfg.data["ai"]["poll_interval"] = 7
    assert orch._ai_poll_interval() == 7.0
    cfg.data["ai"]["poll_interval"] = 0  # 下限 1 秒：防 0 值空转打满 CPU
    assert orch._ai_poll_interval() == 1.0


# ---------- API 面 ----------


def test_preview_and_breaker_api(cfg, guard):
    llm = FakeLLM()
    orch = _orch(cfg, guard, llm, urls=(U1,))
    r, _note = _capture(orch, guard)
    app = create_app(cfg, orchestrator=orch)

    with TestClient(app) as client:
        # 总闸关：409（试跑也受总闸约束）
        assert client.post(f"/api/enrich/preview?entry_id={r['entry_id']}").status_code == 409

        cfg.data["ai"]["enabled"] = True
        # 未知条目 404
        assert client.post("/api/enrich/preview?entry_id=nope").status_code == 404
        # 试跑成功：结构化结果 + 零落盘
        body = client.post(f"/api/enrich/preview?entry_id={r['entry_id']}").json()
        assert body["tags"] == ["python", "知识库"]
        assert body["card_id"] == wiki_card_id(r["entry_id"])
        assert _wiki(guard) == []

        # 手动跑一批：逐条 outcome
        batch = client.post("/api/enrich/run").json()
        assert batch["batch"]["enriched"] == 1
        assert batch["batch"]["results"][0]["outcome"] == "enriched"

        # 熔断运行态出现在 status；reset 端点可清零
        cfg.write_breaker(state="open", consecutive_failures=2, opened_at="t", last_error="e")
        status = client.get("/api/status").json()
        assert status["ai_trigger_mode"] == "manual"
        assert status["ai_breaker"]["state"] == "open"
        assert client.post("/api/enrich/breaker/reset").json()["breaker"]["state"] == "closed"
        assert cfg.data["ai"]["breaker"]["state"] == "closed"


def test_preview_api_reports_llm_failure_as_bad_gateway(cfg, guard):
    llm = FakeLLM()
    llm.fail_tasks = {"tags"}
    orch = _orch(cfg, guard, llm, urls=(U1,))
    r, _note = _capture(orch, guard)
    cfg.data["ai"]["enabled"] = True
    app = create_app(cfg, orchestrator=orch)

    with TestClient(app) as client:
        resp = client.post(f"/api/enrich/preview?entry_id={r['entry_id']}")
        assert resp.status_code == 502
        assert "boom" in resp.json()["detail"]
        assert _wiki(guard) == []  # 上游失败也不产生任何产物


def test_config_api_rejects_invalid_trigger_mode(cfg, guard):
    app = create_app(cfg)
    with TestClient(app) as client:
        bad = client.put("/api/config", json={"config": {"ai": {"trigger_mode": "sometimes"}}})
        assert bad.status_code == 400
        assert "trigger_mode" in bad.json()["detail"]
        ok = client.put("/api/config", json={"config": {"ai": {"trigger_mode": "auto"}}})
        assert ok.status_code == 200
        assert ok.json()["config"]["ai"]["trigger_mode"] == "auto"
