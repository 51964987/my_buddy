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


class FakeLLM:
    """按任务返回预设回复的假客户端工厂；记录 prompt，可注入失败。"""

    def __init__(self, replies=None):
        self.replies = replies or {"tags": TAGS_REPLY, "summary_card": CARD_REPLY}
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

    # wiki 卡：隔离区 draft，带 ai_generated 溯源
    cards = list((guard.kb_root / "wiki").glob("w-*.md"))
    assert len(cards) == 1
    card_fm = _fm(cards[0])
    assert card_fm["id"] == wiki_card_id(r["entry_id"])
    assert card_fm["type"] == "summary"
    assert card_fm["ai_generated"] is True
    assert card_fm["status"] == "draft"
    assert card_fm["sources"] == [r["entry_id"]]
    card_text = cards[0].read_text(encoding="utf-8")
    assert "## 要点" in card_text and "第一段要点" in card_text

    # 正文未被改动，只动了白名单字段
    body = note.read_text(encoding="utf-8").split("---", 2)[2]
    assert "first paragraph" in body

    # 状态汇总可见
    st = orch.status()
    assert st["enrich"]["enriched"] == 1
    assert st["enrich"]["pending"] == 0
    # 任务 prompt 均发出（标签 + 摘要卡）
    assert {t for t, _ in llm.prompts} == {"tags", "summary_card"}


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

    cards = list((guard.kb_root / "wiki").glob("*.md"))
    assert len(cards) == 1  # 确定性 id：同源同类型幂等覆盖，不产生重复卡


def test_wiki_card_no_duplicate_title_heading(cfg, guard):
    """回归：模型自带一级标题时不重复加题目（真实 GLM 冒烟发现）。"""
    llm = FakeLLM(replies={"tags": TAGS_REPLY, "summary_card": json.dumps({"summary": "s", "card": "# seed\n\n## 要点\n- a"}, ensure_ascii=False)})
    from kbserver.enrich import Enricher

    enricher = Enricher(cfg, guard, client_factory=llm)
    rel = _seed_note(guard)
    enricher.enrich_entry(rel)
    card_text = next((guard.kb_root / "wiki").glob("*.md")).read_text(encoding="utf-8")
    assert card_text.count("# seed") == 1

    # 模型不带一级标题 → 模板补加题目
    llm2 = FakeLLM(replies={"tags": TAGS_REPLY, "summary_card": json.dumps({"summary": "s", "card": "## 要点\n- a"}, ensure_ascii=False)})
    enricher2 = Enricher(cfg, guard, client_factory=llm2)
    rel2 = _seed_note(guard, "seed0002")
    enricher2.enrich_entry(rel2)
    card2 = [p for p in (guard.kb_root / "wiki").glob("*.md") if "seed0002" in p.read_text(encoding="utf-8")][0]
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
