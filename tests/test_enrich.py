"""P3 AI 整理流水线测试（§5.1 enrich 实施口径 / §4.5 wiki 卡约定）。

覆盖：normalize → enrich 全链路、字段级白名单、wiki 卡幂等、
重试与断点（attempts 计数 → error 态 → rerun 复活）、ai.enabled 开关。
"""

import json

import pytest

from kbserver.enrich import EnrichError, wiki_card_id
from kbserver.llm import LLMError
from kbserver.orchestrator import Orchestrator

from .conftest import SAMPLE_HTML, make_fetcher

TAGS_REPLY = '```json\n["python", "知识库"]\n```'
CARD_REPLY = json.dumps(
    {"summary": "一篇关于示例页面的短文。", "card": "## 要点\n- 第一段要点\n\n## 细节\n- 第二段细节"},
    ensure_ascii=False,
)
# 实体抽取默认回复（§9 决策 6 v0.40）：两个实体 + 一条合法关系，类型须在默认白名单内
ENTITY_REPLY = json.dumps(
    {
        "entities": [
            {"name": "示例产品", "type": "产品", "aliases": ["Example"]},
            {"name": "示例技术", "type": "技术", "aliases": []},
        ],
        "relations": [{"source": "示例产品", "type": "依赖", "target": "示例技术"}],
    },
    ensure_ascii=False,
)


class FakeLLM:
    """按任务返回预设回复的假客户端工厂；记录 prompt，可注入失败。

    未显式指定的任务回落到默认回复（v0.40 起 enrich 固定请求 entity_extraction，
    存量用例只关注 tags/summary_card，不为此逐个补键）。
    """

    DEFAULT_REPLIES = {
        "tags": TAGS_REPLY,
        "summary_card": CARD_REPLY,
        "entity_extraction": ENTITY_REPLY,
    }

    def __init__(self, replies=None):
        self.replies = {**self.DEFAULT_REPLIES, **(replies or {})}
        self.prompts: list[tuple[str, str]] = []
        self.fail_tasks: set[str] = set()

    def __call__(self, task: str):
        outer = self

        class _Client:
            def chat(self, messages, temperature=0.2):
                if task in outer.fail_tasks:
                    raise LLMError("boom")
                outer.prompts.append((task, messages[0]["content"]))
                return outer.replies[task]

        return _Client()


def _make_orch(cfg, guard, llm, fetcher=None):
    from kbserver.enrich import Enricher

    enricher = Enricher(cfg, guard, client_factory=llm)
    default = make_fetcher(pages={"https://example.com/e1": SAMPLE_HTML})
    return Orchestrator(cfg, guard, fetcher=fetcher or default, enricher=enricher)


def _capture_and_normalize(orch, guard, url="https://example.com/e1"):
    from kbserver.capture import accept_capture

    r = accept_capture(guard, {"url": url, "entry": "cli"})
    orch.scan_once()
    notes = list((guard.kb_root / "sources").rglob("note.md"))
    assert len(notes) == 1
    return r, notes[0]


def _note_rel(guard, note_path):
    return note_path.relative_to(guard.kb_root).as_posix()


def _fm(note_path):
    from kbserver.frontmatter import split_note

    return split_note(note_path.read_text(encoding="utf-8"))[0]


def test_enrich_full_pipeline(cfg, guard):
    cfg.data["ai"]["enabled"] = True
    # 显式锁 provider 指向（不依赖内置默认：默认已是 ollama 本地，v0.18）
    cfg.data["ai"]["tasks"]["tags"] = {"provider": "glm", "model": ""}
    cfg.data["ai"]["tasks"]["summary_card"] = {"provider": "glm", "model": ""}
    llm = FakeLLM()
    orch = _make_orch(cfg, guard, llm)
    r, note = _capture_and_normalize(orch, guard)

    summary = orch.enrich_scan_once()
    assert summary["enriched"] == 1

    fm = _fm(note)
    assert fm["status"] == "enriched"
    assert fm["tags"] == ["python", "知识库"]
    assert fm["ai"]["summary"] == "一篇关于示例页面的短文。"
    assert fm["ai"]["generated_at"]
    assert "glm-4-flash" in fm["ai"]["model"]  # 回落到 provider 默认 model

    # wiki 卡：隔离区 draft，带 ai_generated 溯源（v0.40 起另有实体卡，按 type 过滤摘要卡）
    cards = list((guard.kb_root / "wiki").glob("w-*.md"))
    summary_cards = [p for p in cards if "type: summary" in p.read_text(encoding="utf-8")]
    assert len(summary_cards) == 1
    card_fm = _fm(summary_cards[0])
    assert card_fm["id"] == wiki_card_id(r["entry_id"])
    assert card_fm["type"] == "summary"
    assert card_fm["ai_generated"] is True
    assert card_fm["status"] == "draft"
    assert card_fm["sources"] == [r["entry_id"]]
    card_text = summary_cards[0].read_text(encoding="utf-8")
    assert "## 要点" in card_text and "第一段要点" in card_text
    # 来源行必须是完整可点链接（§4.5 v0.35）：此前写成 `[id]` 残缺语法，渲染后是裸方括号
    assert f"> 来源：[{r['entry_id']}](https://example.com/e1)" in card_text

    # 正文未被改动，只动了白名单字段
    body = note.read_text(encoding="utf-8").split("---", 2)[2]
    assert "first paragraph" in body

    # 状态汇总可见
    st = orch.status()
    assert st["enrich"]["enriched"] == 1
    assert st["enrich"]["pending"] == 0
    # 任务 prompt 均发出（标签 + 摘要卡 + 实体抽取，§9 决策 6 v0.40）
    assert {t for t, _ in llm.prompts} == {"tags", "summary_card", "entity_extraction"}


def test_enrich_disabled_makes_no_calls(cfg, guard):
    llm = FakeLLM()
    orch = _make_orch(cfg, guard, llm)
    _capture_and_normalize(orch, guard)

    summary = orch.enrich_scan_once()
    assert summary["scanned"] == 0
    assert llm.prompts == []
    assert orch.status()["ai_enabled"] is False


def test_enrich_failure_retries_then_error_and_rerun(cfg, guard):
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["max_attempts"] = 2
    llm = FakeLLM()
    llm.fail_tasks = {"tags", "summary_card"}
    orch = _make_orch(cfg, guard, llm)
    _capture_and_normalize(orch, guard)
    note = list((guard.kb_root / "sources").rglob("note.md"))[0]

    s1 = orch.enrich_scan_once()
    assert s1["retry"] == 1
    assert _fm(note)["status"] == "normalized"  # 未超限不转 error
    meta = json.loads((note.parent / "meta.json").read_text(encoding="utf-8"))
    assert meta["enrich"]["attempts"] == 1

    s2 = orch.enrich_scan_once()
    assert s2["error"] == 1
    assert _fm(note)["status"] == "error"
    meta = json.loads((note.parent / "meta.json").read_text(encoding="utf-8"))
    assert meta["error_stage"] == "enrich"
    assert "boom" in meta["error_message"]
    assert orch.status()["enrich"]["errors"], "enrich error must be visible in status"

    # 超限后不再重试（无目标）
    s3 = orch.enrich_scan_once()
    assert s3["scanned"] == 0

    # rerun 复活 → 重试计数清零、状态回 normalized；恢复后成功
    assert orch.rerun_enrich(_fm(note)["id"]) is True
    assert _fm(note)["status"] == "normalized"
    meta = json.loads((note.parent / "meta.json").read_text(encoding="utf-8"))
    assert meta["enrich"]["attempts"] == 0
    assert "error_stage" not in meta

    llm.fail_tasks.clear()
    s4 = orch.enrich_scan_once()
    assert s4["enriched"] == 1
    assert _fm(note)["status"] == "enriched"


def test_wiki_card_idempotent_overwrite(cfg, guard):
    cfg.data["ai"]["enabled"] = True
    llm = FakeLLM()
    orch = _make_orch(cfg, guard, llm)
    _capture_and_normalize(orch, guard)
    orch.enrich_scan_once()

    note = list((guard.kb_root / "sources").rglob("note.md"))[0]
    # 模拟重跑：状态重置回 normalized 后再次 enrich
    orch.guard.patch_note_fields("enrich", _note_rel(guard, note), {"status": "normalized"}, {"status"})
    orch.enricher.enrich_entry(_note_rel(guard, note))

    cards = [p for p in (guard.kb_root / "wiki").glob("*.md") if "type: summary" in p.read_text(encoding="utf-8")]
    assert len(cards) == 1  # 确定性 id：同源同类型幂等覆盖，不产生重复卡（实体卡另行断言）


def test_wiki_card_no_duplicate_title_heading(cfg, guard):
    """回归：模型自带一级标题时不重复加题目（真实 GLM 冒烟发现）。"""
    llm = FakeLLM(replies={"tags": TAGS_REPLY, "summary_card": json.dumps({"summary": "s", "card": "# seed\n\n## 要点\n- a"}, ensure_ascii=False)})
    from kbserver.enrich import Enricher

    enricher = Enricher(cfg, guard, client_factory=llm)
    rel = _seed_note(guard)
    enricher.enrich_entry(rel)
    card_text = [p for p in (guard.kb_root / "wiki").glob("*.md") if "type: summary" in p.read_text(encoding="utf-8")][0].read_text(encoding="utf-8")
    assert card_text.count("# seed") == 1

    # 模型不带一级标题 → 模板补加题目
    llm2 = FakeLLM(replies={"tags": TAGS_REPLY, "summary_card": json.dumps({"summary": "s", "card": "## 要点\n- a"}, ensure_ascii=False)})
    enricher2 = Enricher(cfg, guard, client_factory=llm2)
    rel2 = _seed_note(guard, "seed0002")
    enricher2.enrich_entry(rel2)
    card2 = [p for p in (guard.kb_root / "wiki").glob("*.md") if "type: summary" in p.read_text(encoding="utf-8") and "seed0002" in p.read_text(encoding="utf-8")][0]
    assert "# seed" in card2.read_text(encoding="utf-8")


def _seed_note(guard, entry_id="seed0001"):
    rel = f"sources/web/2026/{entry_id}/note.md"
    guard.write_text(
        "normalize",
        rel,
        "---\n"
        f"id: {entry_id}\n"
        "title: seed\n"
        "status: normalized\n"
        "tags: []\n"
        "---\n\n正文内容。\n",
    )
    return rel


def test_enrich_non_json_reply_raises(cfg, guard):
    llm = FakeLLM(replies={"tags": "我觉得标签是 python", "summary_card": CARD_REPLY})
    from kbserver.enrich import Enricher

    enricher = Enricher(cfg, guard, client_factory=llm)
    rel = _seed_note(guard)
    with pytest.raises(EnrichError):
        enricher.enrich_entry(rel)


def test_enricher_rejects_missing_summary(cfg, guard):
    llm = FakeLLM(replies={"tags": TAGS_REPLY, "summary_card": json.dumps({"card": "只有卡片"})})
    from kbserver.enrich import Enricher

    enricher = Enricher(cfg, guard, client_factory=llm)
    rel = _seed_note(guard)
    with pytest.raises(EnrichError):
        enricher.enrich_entry(rel)


def test_confidence_written_to_card_and_entry(cfg, guard):
    """置信度（§4.5 v0.17）：模型合法输出 → 卡 frontmatter 与源条目 ai.confidence 同值。"""
    llm = FakeLLM(
        replies={
            "tags": TAGS_REPLY,
            "summary_card": json.dumps({"summary": "s", "confidence": 0.87, "card": "## 要点\n- a"}, ensure_ascii=False),
        }
    )
    from kbserver.enrich import Enricher

    enricher = Enricher(cfg, guard, client_factory=llm)
    rel = _seed_note(guard)
    enricher.enrich_entry(rel)
    card_fm = _fm(next((guard.kb_root / "wiki").glob("*.md")))
    assert card_fm["confidence"] == 0.87
    fm = _fm(guard.kb_root.joinpath(*rel.split("/")))
    assert fm["ai"]["confidence"] == 0.87


def test_confidence_omitted_when_invalid(cfg, guard):
    """模型未给或非法置信度 → 不写字段、不造默认值。"""
    llm = FakeLLM(
        replies={
            "tags": TAGS_REPLY,
            "summary_card": json.dumps({"summary": "s", "confidence": 1.5, "card": "## 要点\n- a"}, ensure_ascii=False),
        }
    )
    from kbserver.enrich import Enricher

    enricher = Enricher(cfg, guard, client_factory=llm)
    rel = _seed_note(guard)
    enricher.enrich_entry(rel)
    card_fm = _fm(next((guard.kb_root / "wiki").glob("*.md")))
    assert "confidence" not in card_fm
    fm = _fm(guard.kb_root.joinpath(*rel.split("/")))
    assert "confidence" not in fm["ai"]


def test_enrich_log_records_outcomes(cfg, guard):
    """操作流水（§4.4 v0.17）：成功与失败均记 meta.json enrich.log（含 rerun）。"""
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["max_attempts"] = 1
    llm = FakeLLM()
    llm.fail_tasks = {"tags"}
    orch = _make_orch(cfg, guard, llm)
    _capture_and_normalize(orch, guard)
    note = list((guard.kb_root / "sources").rglob("note.md"))[0]
    note_rel = _note_rel(guard, note)

    orch.enrich_scan_once()  # fail → error（超限）
    meta = json.loads((note.parent / "meta.json").read_text(encoding="utf-8"))
    log = meta["enrich"]["log"]
    assert log[-1]["outcome"] == "error"
    assert "boom" in log[-1]["message"]
    assert log[-1]["attempts"] == 1

    assert orch.rerun_enrich(_fm(note)["id"]) is True
    meta = json.loads((note.parent / "meta.json").read_text(encoding="utf-8"))
    assert meta["enrich"]["log"][-1]["outcome"] == "rerun"

    llm.fail_tasks.clear()
    orch.enrich_scan_once()
    meta = json.loads((note.parent / "meta.json").read_text(encoding="utf-8"))
    assert meta["enrich"]["log"][-1]["outcome"] == "enriched"


def test_enrich_concurrency_sequential_default(cfg, guard):
    """并发度默认 1：顺序执行，结果与并发路径一致（§11.5 ai.concurrency）。"""
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["concurrency"] = 2
    llm = FakeLLM()
    orch = _make_orch(cfg, guard, llm)
    _capture_and_normalize(orch, guard)
    # 再造一个目标
    from kbserver.enrich import Enricher

    _seed_note(guard, "seed0002")
    summary = orch.enrich_scan_once()
    assert summary["scanned"] == 2
    assert summary["enriched"] == 2


# ---------- 实体抽取（§9 决策 6 v0.40 / §4.5 实体卡约定） ----------

def test_entity_extraction_writes_entity_cards(cfg, guard):
    """实体卡落盘：frontmatter 结构 + 正文构成纪律（类型/关系/末尾来源行）。"""
    from kbserver.enrich import Enricher, entity_card_id

    enricher = Enricher(cfg, guard, client_factory=FakeLLM())
    rel = _seed_note(guard)
    result = enricher.enrich_entry(rel)

    assert result["entity_cards"] == [f"wiki/{entity_card_id('示例产品')}.md", f"wiki/{entity_card_id('示例技术')}.md"]
    card = guard.kb_root.joinpath(*result["entity_cards"][0].split("/"))
    from kbserver.frontmatter import split_note

    fm, body = split_note(card.read_text(encoding="utf-8"))
    assert fm["type"] == "entity" and fm["ai_generated"] is True and fm["status"] == "draft"
    assert fm["name"] == "示例产品" and fm["entity_type"] == "产品"
    assert fm["aliases"] == ["Example"]
    assert fm["relations"] == [{"type": "依赖", "target": entity_card_id("示例技术"), "name": "示例技术"}]
    assert fm["sources"] == ["seed0001"]
    # 正文构成：模板一级标题 + 结构化信息行 + 来源行（无 url 降级为纯文本）
    assert body.startswith("# 示例产品")
    assert "- 类型：产品" in body
    assert "- 别名：Example" in body
    assert f"- 关系：依赖 → [示例技术](./{entity_card_id('示例技术')}.md)" in body
    assert "> 来源条目：seed0001" in body


def test_entity_schema_enforced(cfg, guard):
    """受控 schema（§5.1 v0.40）：越界实体类型丢弃、孤儿关系丢弃、同名取首个。"""
    from kbserver.enrich import Enricher, entity_card_id

    reply = json.dumps(
        {
            "entities": [
                {"name": "好实体", "type": "产品", "aliases": []},
                {"name": "坏实体", "type": "外星文明"},  # 类型越界 → 丢弃
                {"name": "好实体", "type": "技术"},  # 同名取首个 → 类型保持"产品"
            ],
            "relations": [
                {"source": "好实体", "type": "依赖", "target": "坏实体"},  # 孤儿 → 丢弃
                {"source": "好实体", "type": "魔法", "target": "好实体"},  # 关系类型越界 → 丢弃
                {"source": "好实体", "type": "相关", "target": "好实体"},  # 自环合法，保留
            ],
        },
        ensure_ascii=False,
    )
    enricher = Enricher(cfg, guard, client_factory=FakeLLM(replies={"entity_extraction": reply}))
    rel = _seed_note(guard)
    enricher.enrich_entry(rel)

    assert not (guard.kb_root / "wiki" / f"{entity_card_id('坏实体')}.md").exists()
    fm = _fm(guard.kb_root / "wiki" / f"{entity_card_id('好实体')}.md")
    assert fm["entity_type"] == "产品"  # 同名取首个，未被第二个覆盖
    assert fm["relations"] == [{"type": "相关", "target": entity_card_id("好实体"), "name": "好实体"}]


def test_entity_name_sanity_rejects_template_parroting(cfg, guard):
    """实体名质量校验（v0.54 机械防线，§5.1）：模板词/与类型同名/单字名丢弃，
    含模板词的别名过滤——防小模型复读 prompt 示例占位词入库
    （实测 minicpm5 产出「产品名/人物名/地点名」等 8 张模板卡）。"""
    from kbserver.enrich import Enricher, entity_card_id

    reply = json.dumps(
        {
            "entities": [
                {"name": "产品名", "type": "产品", "aliases": []},  # 模板占位词
                {"name": "技术", "type": "技术", "aliases": []},  # 与实体类型同名
                {"name": "云", "type": "技术", "aliases": []},  # 单字（长度不足 2）
                {"name": "ByteHouse", "type": "产品", "aliases": ["产品名，可省略", "BH"]},  # 正常实体
                {"name": "缺损", "type": "概念", "aliases": []},  # 类型越界（v0.58 默认白名单已移除「概念」）→ 丢弃
            ],
            "relations": [],
        },
        ensure_ascii=False,
    )
    enricher = Enricher(cfg, guard, client_factory=FakeLLM(replies={"entity_extraction": reply}))
    rel = _seed_note(guard)
    result = enricher.enrich_entry(rel)

    assert not (guard.kb_root / "wiki" / f"{entity_card_id('产品名')}.md").exists()
    assert not (guard.kb_root / "wiki" / f"{entity_card_id('技术')}.md").exists()
    assert not (guard.kb_root / "wiki" / f"{entity_card_id('云')}.md").exists()
    assert not (guard.kb_root / "wiki" / f"{entity_card_id('缺损')}.md").exists()
    fm = _fm(guard.kb_root / "wiki" / f"{entity_card_id('ByteHouse')}.md")
    assert fm["aliases"] == ["BH"]  # 含模板词的别名被过滤，真实别名保留
    assert result["entities_discarded"] == 3


def test_entity_merge_idempotent(cfg, guard):
    """同名确定性合并（§4.5 v0.40）：两个条目抽到同一实体 → 同一张卡，
    sources/aliases/relations 并集去重，created_at 保留首次。"""
    from kbserver.enrich import Enricher, entity_card_id

    reply = json.dumps(
        {
            "entities": [{"name": "共享实体", "type": "技术", "aliases": ["别名一"]}],
            "relations": [{"source": "共享实体", "type": "相关", "target": "共享实体"}],
        },
        ensure_ascii=False,
    )
    enricher = Enricher(cfg, guard, client_factory=FakeLLM(replies={"entity_extraction": reply}))
    rel1 = _seed_note(guard, "seed0001")
    enricher.enrich_entry(rel1)
    card_path = guard.kb_root / "wiki" / f"{entity_card_id('共享实体')}.md"
    fm1 = _fm(card_path)
    created = fm1["created_at"]

    # 第二个条目给出不同别名与同一条关系 → 合并而非重复
    reply2 = json.dumps(
        {
            "entities": [{"name": "共享实体", "type": "技术", "aliases": ["别名二", "别名一"]}],
            "relations": [{"source": "共享实体", "type": "相关", "target": "共享实体"}],
        },
        ensure_ascii=False,
    )
    enricher2 = Enricher(cfg, guard, client_factory=FakeLLM(replies={"entity_extraction": reply2}))
    rel2 = _seed_note(guard, "seed0002")
    enricher2.enrich_entry(rel2)

    wiki_files = list((guard.kb_root / "wiki").glob("*.md"))
    entity_cards = [p for p in wiki_files if p.name.startswith("w-") and "共享实体" in p.read_text(encoding="utf-8")]
    assert len(entity_cards) == 1  # 同名同卡，未产生重复卡
    fm = _fm(card_path)
    assert fm["sources"] == ["seed0001", "seed0002"]
    assert fm["aliases"] == ["别名一", "别名二"]
    assert len(fm["relations"]) == 1  # (type, target) 去重
    assert fm["created_at"] == created  # 保留首次
    assert fm["status"] == "draft"


def test_entity_promoted_not_downgraded(cfg, guard):
    """重跑不降级：已 promoted 的实体卡重跑后 status 仍为 promoted、created_at 不变。"""
    from kbserver.enrich import Enricher, entity_card_id

    enricher = Enricher(cfg, guard, client_factory=FakeLLM())
    rel = _seed_note(guard)
    enricher.enrich_entry(rel)
    card_path = guard.kb_root / "wiki" / f"{entity_card_id('示例产品')}.md"
    created = _fm(card_path)["created_at"]
    # 模拟人工晋升（curation 守卫通道）
    guard.patch_note_fields("curation", f"wiki/{entity_card_id('示例产品')}.md", {"status": "promoted"}, {"status"})

    enricher.enrich_entry(rel)  # 重跑（同条目幂等覆盖）
    fm = _fm(card_path)
    assert fm["status"] == "promoted"
    assert fm["created_at"] == created
    assert fm["sources"] == ["seed0001"]  # 不重复追加


def test_entity_empty_whitelist_skips_task(cfg, guard):
    """空白名单 = 未定义 schema（config.py 口径）：跳过任务、不发起实体抽取调用。"""
    from kbserver.enrich import Enricher

    cfg.data["ai"]["kg"]["entity_types"] = []
    llm = FakeLLM()
    enricher = Enricher(cfg, guard, client_factory=llm)
    rel = _seed_note(guard)
    result = enricher.enrich_entry(rel)

    assert result["entity_cards"] == []
    assert all(task != "entity_extraction" for task, _ in llm.prompts)
    # 摘要卡与标签照常
    assert (guard.kb_root / "wiki").exists()


def test_entity_extraction_failure_fails_entry(cfg, guard):
    """实体抽取失败 = 整条 enrich 失败（追加任务同口径，交编排器重试/转 error）。"""
    from kbserver.enrich import Enricher

    llm = FakeLLM()
    llm.fail_tasks = {"entity_extraction"}
    enricher = Enricher(cfg, guard, client_factory=llm)
    rel = _seed_note(guard)
    with pytest.raises(EnrichError):
        enricher.enrich_entry(rel)


def test_preview_includes_entities_without_writing(cfg, guard):
    """试跑（§5.1 v0.29/v0.40）：plan 返回实体结果且零落盘。"""
    from kbserver.enrich import Enricher

    llm = FakeLLM()
    enricher = Enricher(cfg, guard, client_factory=llm)
    rel = _seed_note(guard)
    plan = enricher.plan_entry(rel)

    assert [e["name"] for e in plan["entities"]] == ["示例产品", "示例技术"]
    assert plan["entity_model"]  # 实体抽取任务实际生效模型
    assert not (guard.kb_root / "wiki").exists()  # 零落盘
