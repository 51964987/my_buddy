"""概念聚合（§5.1 v0.40 ②，v0.56 落地 aggregate stage）回归用例。

覆盖：concept_card 未配置跳过（不计熔断）/ plan 解析强校验（模板标题、无效引用、
空引用、同名去重）/ run 落盘与幂等合并（promoted 不降级、sources 并集、
created_at 保留首次）/ 试跑零落盘 / 编排器手动与 auto 门控 + 熔断记账 / API 端点。
"""

import json

import yaml

from kbserver.enrich import Enricher, concept_card_id, entity_card_id
from kbserver.orchestrator import Orchestrator

from .conftest import make_fetcher


class FakeLLM:
    """按任务返回预设回复的假客户端工厂；chat_raises=True 时模拟模型故障。"""

    def __init__(self, replies=None, bad_reply=False):
        self.replies = replies or {}
        self.bad_reply = bad_reply
        self.calls: list[str] = []

    def __call__(self, task):
        self.calls.append(task)

        class _C:
            def chat(self, messages, temperature=0.2):
                if self.outer.bad_reply:
                    return "模型抽风输出，不是 JSON"
                return self.outer.replies[task]

        _C.outer = self
        return _C()


def _make_orch(cfg, guard, llm):
    enricher = Enricher(cfg, guard, client_factory=llm)
    cfg.data["pipeline"]["worker_enabled"] = False  # 测试 inline 执行，不起线程
    return Orchestrator(cfg, guard, fetcher=make_fetcher(pages={}), enricher=enricher)


def _configure_concept_task(cfg):
    cfg.data["ai"]["tasks"]["concept_card"] = {"provider": "ollama", "model": ""}


def _seed_enriched(guard, entry_id="seed0001"):
    rel = f"sources/web/2026/{entry_id}/note.md"
    fm = {
        "id": entry_id,
        "title": f"t-{entry_id}",
        "status": "enriched",
        "url": f"https://example.com/{entry_id}",
        "ai": {"summary": f"摘要{entry_id}"},
    }
    guard.write_text(
        "normalize", rel, "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n正文。\n"
    )
    return rel


def _seed_entity(guard, name, sources=("seed0001",), status="draft", etype="技术"):
    cid = entity_card_id(name)
    fm = {
        "id": cid,
        "type": "entity",
        "ai_generated": True,
        "status": status,
        "name": name,
        "aliases": [],
        "entity_type": etype,
        "relations": [],
        "sources": list(sources),
        "created_at": "2026-10-05T10:00:00+08:00",
        "model": "fake-model",
    }
    guard.write_text(
        "enrich",
        f"wiki/{cid}.md",
        "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + f"# {name}\n\n- 类型：{etype}\n",
    )
    return cid


def _concept_reply(*concepts):
    return json.dumps({"concepts": list(concepts)}, ensure_ascii=False)


# ---------- plan 层 ----------


def test_plan_skips_when_task_not_configured(cfg, guard):
    """concept_card 任务未配置 = 聚合整体跳过（用户选择非故障，不计熔断）。"""
    llm = FakeLLM()
    orch = _make_orch(cfg, guard, llm)

    plan = orch.enricher.plan_concept()
    assert plan == {"skipped": "concept_card_not_configured"}
    assert llm.calls == []  # 未发起任何 LLM 调用

    cfg.data["ai"]["enabled"] = True  # 总闸开才走到聚合指令（关闸时返回 enabled=False）
    r = orch.aggregate_run()
    assert r["skipped_reason"] == "concept_card_not_configured"
    auto = orch.aggregate_auto_once()  # 门控（manual）先拦，改 auto 后也应跳过不计熔断
    assert auto.get("skipped_reason") == "gated"
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["trigger_mode"] = "auto"
    auto = orch.aggregate_auto_once()
    assert auto["skipped_reason"] == "concept_card_not_configured"
    assert cfg.data["ai"]["breaker"]["consecutive_failures"] == 0


def test_plan_validates_titles_and_refs(cfg, guard):
    """解析强校验：模板标题丢弃 / 无效引用过滤 / 双空引用整体丢弃 / 同名去重。"""
    _configure_concept_task(cfg)
    _seed_enriched(guard, "seed0001")
    _seed_enriched(guard, "seed0002")
    _seed_entity(guard, "ByteHouse")
    llm = FakeLLM(
        replies={
            "concept_card": _concept_reply(
                {  # 合法：实体 + 条目引用都有效
                    "title": "列式存储引擎",
                    "summary": "s",
                    "card": "## 要点",
                    "entity_refs": ["ByteHouse"],
                    "entry_refs": ["seed0001"],
                    "confidence": 0.8,
                },
                {"title": "概念名", "card": "x", "entity_refs": ["ByteHouse"]},  # 模板占位标题（v0.54 黑名单词）
                {"title": "幽灵概念", "card": "x", "entity_refs": ["不存在"], "entry_refs": ["ghost"]},  # 双空引用
                {  # 无效条目引用被过滤，实体引用仍有效 → 保留
                    "title": "部分有效",
                    "card": "x",
                    "entity_refs": ["bytehouse"],  # 大小写不敏感解析
                    "entry_refs": ["ghost"],
                },
                {"title": "列式存储引擎", "card": "y", "entity_refs": ["ByteHouse"]},  # 同名（小写）去重取首个
            )
        }
    )
    enricher = Enricher(cfg, guard, client_factory=llm)

    plan = enricher.plan_concept()
    titles = [c["title"] for c in plan["concepts"]]
    assert titles == ["列式存储引擎", "部分有效"]
    assert plan["discarded"] == 3  # 模板标题 + 双空引用 + 重名（同名去重计入丢弃）
    assert plan["inputs"] == {"entities": 1, "entries": 2}
    first = plan["concepts"][0]
    assert first["card_id"] == concept_card_id("列式存储引擎")
    assert first["entity_card_ids"] == [entity_card_id("ByteHouse")]
    assert first["entry_ids"] == ["seed0001"]
    assert plan["model"]  # 实际生效后端已解析


def test_concept_card_id_deterministic():
    assert concept_card_id("列式存储") == concept_card_id(" 列式存储 ")
    assert concept_card_id("A") != concept_card_id("B")


# ---------- run 层 ----------


def test_run_writes_draft_card_via_aggregate_stage(cfg, guard):
    _configure_concept_task(cfg)
    _seed_enriched(guard, "seed0001")
    _seed_entity(guard, "ByteHouse", sources=("seed0001", "seed0002"))
    llm = FakeLLM(
        replies={
            "concept_card": _concept_reply(
                {"title": "列式存储", "summary": "s", "card": "## 要点", "entity_refs": ["ByteHouse"], "entry_refs": ["seed0001"]}
            )
        }
    )
    enricher = Enricher(cfg, guard, client_factory=llm)

    result = enricher.run_concept(enricher.plan_concept())
    assert len(result["written"]) == 1
    w = result["written"][0]
    assert w["status"] == "draft"  # 自动产物一律 draft，永不自动晋升

    from kbserver.frontmatter import split_note

    fm, body = split_note(guard.kb_root.joinpath(*w["rel"].split("/")).read_text(encoding="utf-8"))
    assert fm["type"] == "concept" and fm["ai_generated"] is True
    # sources 并集：本轮 entry_refs + 被引实体卡的来源条目
    assert fm["sources"] == ["seed0001", "seed0002"]
    assert fm["confidence"] == 0.8 if "confidence" in fm else True
    # 正文构成纪律：模板标题 + 模型产出 + 逐条可点来源行（seed0001 有 url）
    assert body.startswith("# 列式存储")
    assert "> 来源：[seed0001](https://example.com/seed0001)" in body
    assert "> 来源条目：seed0002" in body


def test_run_merge_idempotent_promoted_not_downgraded(cfg, guard):
    """重跑幂等：promoted 不降级、sources 并集、created_at 保留首次。"""
    _configure_concept_task(cfg)
    _seed_enriched(guard, "seed0001")
    _seed_enriched(guard, "seed0002")
    _seed_entity(guard, "ByteHouse")
    llm = FakeLLM(
        replies={
            "concept_card": _concept_reply(
                {"title": "列式存储", "card": "x", "entity_refs": ["ByteHouse"], "entry_refs": ["seed0001"]}
            )
        }
    )
    enricher = Enricher(cfg, guard, client_factory=llm)
    enricher.run_concept(enricher.plan_concept())

    rel = f"wiki/{concept_card_id('列式存储')}.md"
    path = guard.kb_root.joinpath(*rel.split("/"))
    from kbserver.frontmatter import split_note

    fm, _ = split_note(path.read_text(encoding="utf-8"))
    created = fm["created_at"]
    # 人工晋升 + 追加一条新来源（模拟第二来源条目被引用）
    fm["status"] = "promoted"
    fm["sources"] = ["seed0003"]
    guard.write_text("curation", rel, "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n正文\n")
    guard.write_text(
        "normalize",
        "sources/web/2026/seed0003/note.md",
        "---\nid: seed0003\ntitle: t3\nstatus: enriched\n---\n\n正文\n",
    )

    enricher.run_concept(enricher.plan_concept())
    fm2, _ = split_note(path.read_text(encoding="utf-8"))
    assert fm2["status"] == "promoted"  # 不降级回 draft
    assert fm2["created_at"] == created  # 保留首次
    assert fm2["sources"] == ["seed0003", "seed0001"]  # 并集去重，不丢人工追加来源


def test_preview_zero_write(cfg, guard):
    """试跑零落盘：plan 不写 wiki/、不改条目。"""
    _configure_concept_task(cfg)
    _seed_enriched(guard, "seed0001")
    _seed_entity(guard, "ByteHouse")
    llm = FakeLLM(
        replies={
            "concept_card": _concept_reply(
                {"title": "列式存储", "card": "x", "entity_refs": ["ByteHouse"], "entry_refs": ["seed0001"]}
            )
        }
    )
    enricher = Enricher(cfg, guard, client_factory=llm)
    plan = enricher.plan_concept()
    assert plan["concepts"]
    # wiki/ 只有那张实体卡，没有新文件
    assert [p.name for p in (guard.kb_root / "wiki").glob("*.md")] == [f"{entity_card_id('ByteHouse')}.md"]


# ---------- 编排器：门控与熔断 ----------


def test_aggregate_run_manual_not_gated_by_trigger_mode(cfg, guard):
    """手动聚合不受 trigger_mode/熔断门控；仅受 ai.enabled 总闸。"""
    _configure_concept_task(cfg)
    _seed_enriched(guard, "seed0001")
    _seed_entity(guard, "ByteHouse")
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["trigger_mode"] = "manual"
    cfg.data["ai"]["breaker"]["state"] = "open"  # 熔断中手动仍可用
    llm = FakeLLM(
        replies={
            "concept_card": _concept_reply(
                {"title": "列式存储", "card": "x", "entity_refs": ["ByteHouse"], "entry_refs": ["seed0001"]}
            )
        }
    )
    orch = _make_orch(cfg, guard, llm)
    r = orch.aggregate_run()
    assert r["enabled"] is True and len(r["written"]) == 1

    # 总闸关 = 拒绝（API 层映射 409）
    cfg.data["ai"]["enabled"] = False
    assert orch.aggregate_run() == {"enabled": False}


def test_aggregate_auto_breaker_accounting(cfg, guard):
    """auto 轮失败计入熔断（含整轮异常）；成功清零。"""
    _configure_concept_task(cfg)
    _seed_enriched(guard, "seed0001")
    _seed_entity(guard, "ByteHouse")
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["trigger_mode"] = "auto"
    cfg.data["ai"]["breaker_threshold"] = 2

    bad = FakeLLM(bad_reply=True)  # 非 JSON → EnrichError
    orch = _make_orch(cfg, guard, bad)
    r = orch.aggregate_auto_once()
    assert "error" in r
    assert cfg.data["ai"]["breaker"]["consecutive_failures"] == 1
    orch.aggregate_auto_once()
    assert cfg.data["ai"]["breaker"]["state"] == "open"  # 达阈值熔断

    # 熔断期间 auto 被门控拦住，不再累计；显式 reset 后成功轮清零
    assert orch.aggregate_auto_once() == {"skipped_reason": "gated"}
    orch.breaker_reset()
    good = FakeLLM(
        replies={
            "concept_card": _concept_reply(
                {"title": "列式存储", "card": "x", "entity_refs": ["ByteHouse"], "entry_refs": ["seed0001"]}
            )
        }
    )
    orch.enricher = Enricher(cfg, guard, client_factory=good)
    r = orch.aggregate_auto_once()
    assert r["written"]
    assert cfg.data["ai"]["breaker"]["consecutive_failures"] == 0


def test_enrich_auto_once_runs_aggregate_after_batch(cfg, guard):
    """auto 轮在 enrich 批次后执行聚合（§5.1 ②）：summary 带出 aggregate 结果。

    空库（无实体卡无 enriched 条目）= 无目标 → skipped，不抛错、不计熔断
    （对齐 enrich「无目标不参与熔断判定」；冒烟实测空库 run 曾穿透 500，回归）。"""
    _configure_concept_task(cfg)
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["trigger_mode"] = "auto"
    llm = FakeLLM(replies={"concept_card": _concept_reply()})
    orch = _make_orch(cfg, guard, llm)
    summary = orch.enrich_auto_once()
    assert "aggregate" in summary
    assert summary["aggregate"] == {"skipped_reason": "nothing_to_aggregate"}
    assert cfg.data["ai"]["breaker"]["consecutive_failures"] == 0  # 无目标不计熔断

    # run/preview 同口径返回 skipped（200），不穿透
    assert orch.aggregate_run()["skipped_reason"] == "nothing_to_aggregate"
    assert orch.aggregate_preview() == {"skipped": "nothing_to_aggregate"}


# ---------- API ----------


def test_aggregate_api_endpoints(cfg, guard):
    from fastapi.testclient import TestClient

    from kbserver.app import create_app

    _configure_concept_task(cfg)
    _seed_enriched(guard, "seed0001")
    _seed_entity(guard, "ByteHouse")
    cfg.data["ai"]["enabled"] = False
    llm = FakeLLM(
        replies={
            "concept_card": _concept_reply(
                {"title": "列式存储", "card": "x", "entity_refs": ["ByteHouse"], "entry_refs": ["seed0001"]}
            )
        }
    )
    orch = _make_orch(cfg, guard, llm)
    app = create_app(cfg, orchestrator=orch)
    with TestClient(app) as client:
        # 总闸关 → 409（run 与 preview 一致）
        assert client.post("/api/aggregate/run").status_code == 409
        assert client.post("/api/aggregate/preview").status_code == 409

        cfg.data["ai"]["enabled"] = True
        preview = client.post("/api/aggregate/preview").json()
        assert preview["concepts"][0]["title"] == "列式存储"
        assert len([p for p in (guard.kb_root / "wiki").glob("*.md")]) == 1  # 试跑零落盘

        r = client.post("/api/aggregate/run").json()
        assert len(r["written"]) == 1
        assert client.get("/api/wiki").json()["total"] == 2  # 实体卡 + 概念卡
