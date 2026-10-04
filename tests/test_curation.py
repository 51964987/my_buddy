"""人工处置通道（§4.4 v0.17 curation）与只读聚合回归用例。

覆盖：wiki 卡清单/详情、三处置（晋升/打回重生成/删除）、enrich 操作流水聚合、
越界行为（promoted 不能重复晋升、不存在卡 404 裁决）。
"""

import json

import yaml

from kbserver import curation


def _seed_note(guard, entry_id="seed0001", status="enriched", meta=None):
    rel = f"sources/web/2026/{entry_id}/note.md"
    fm = {"id": entry_id, "title": f"t-{entry_id}", "status": status, "tags": ["x"]}
    guard.write_text(
        "normalize",
        rel,
        "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n正文。\n",
    )
    m = {"enrich": {"attempts": 2}}
    if meta:
        m.update(meta)
    guard.write_json("enrich", f"sources/web/2026/{entry_id}/meta.json", m)
    return rel


def _make_card(guard, card_id, sources, status="draft", body="# 摘要卡标题\n\n要点内容。"):
    fm = {
        "id": card_id,
        "type": "summary",
        "ai_generated": True,
        "status": status,
        "sources": sources,
        "confidence": 0.9,
        "created_at": "2026-10-03T10:00:00+08:00",
        "model": "fake-model",
    }
    rel = f"wiki/{card_id}.md"
    guard.write_text(
        "enrich",
        rel,
        "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + body,
    )
    return rel


def test_list_and_detail_with_ai_markers(cfg, guard):
    _make_card(guard, "w-a1", ["seed0001"])
    _make_card(guard, "w-b2", ["seed0002"], status="promoted")

    cards = curation.list_cards(guard)
    assert {c["id"] for c in cards} == {"w-a1", "w-b2"}
    draft = next(c for c in cards if c["id"] == "w-a1")
    assert draft["ai_generated"] is True
    assert draft["status"] == "draft"
    assert draft["confidence"] == 0.9
    assert draft["title"] == "摘要卡标题"  # 题目取正文一级标题

    # status 过滤
    assert [c["id"] for c in curation.list_cards(guard, status="promoted")] == ["w-b2"]
    # 详情含正文
    detail = curation.get_card(guard, "w-a1")
    assert "要点内容" in detail["body"]
    assert curation.get_card(guard, "w-nope") is None


def test_promote_only_from_draft(cfg, guard):
    _make_card(guard, "w-a1", ["seed0001"])
    rel, fm = curation._require_card(guard, "w-a1")
    assert fm["status"] == "draft"

    assert curation.promote_card(guard, "w-a1") is True
    _, fm = curation._require_card(guard, "w-a1")
    assert fm["status"] == "promoted"

    # promoted 不可重复晋升（幂等拒绝，非静默）
    assert curation.promote_card(guard, "w-a1") is False


def test_delete_card(cfg, guard):
    _make_card(guard, "w-a1", ["seed0001"])
    assert curation.delete_card(guard, "w-a1") is True
    assert not guard.exists("wiki/w-a1.md")
    assert curation.list_cards(guard) == []


def test_delete_missing_card_raises(cfg, guard):
    import pytest

    with pytest.raises(KeyError):
        curation.delete_card(guard, "w-nope")


def test_regenerate_resets_source(cfg, guard):
    """打回重生成：删卡 + 源条目回 normalized、重试计数清零、error 详情清除。"""
    _seed_note(guard, "seed0001", status="enriched", meta={"error_stage": "enrich", "error_message": "boom"})
    _make_card(guard, "w-a1", ["seed0001"])

    result = curation.regenerate_card(guard, "w-a1")
    assert result == {"deleted": "w-a1", "reset_sources": ["seed0001"]}
    assert not guard.exists("wiki/w-a1.md")

    note = guard.kb_root.joinpath(*"sources/web/2026/seed0001/note.md".split("/"))
    from kbserver.frontmatter import split_note

    fm, _ = split_note(note.read_text(encoding="utf-8"))
    assert fm["status"] == "normalized"
    meta = json.loads(
        guard.kb_root.joinpath(*"sources/web/2026/seed0001/meta.json".split("/")).read_text("utf-8")
    )
    assert meta["enrich"]["attempts"] == 0
    assert "error_stage" not in meta


def test_regenerate_missing_source_still_deletes(cfg, guard):
    """源条目已被人工删除：删卡即达成，不做多余动作。"""
    _make_card(guard, "w-a1", ["ghost"])
    result = curation.regenerate_card(guard, "w-a1")
    assert result["reset_sources"] == []
    assert not guard.exists("wiki/w-a1.md")


def test_edit_draft_card_marks_edited_at(cfg, guard):
    """修订 draft 卡（§4.5 v0.35，edit-before-accept）：正文替换 + edited_at 留痕，
    ai_generated/status 不变（"人工修订过"只加时间戳，不篡改 AI 溯源标记）。"""
    _make_card(guard, "w-a1", ["seed0001"])
    assert curation.get_card(guard, "w-a1")["edited_at"] is None  # 纯 AI 产出

    out = curation.edit_card(guard, "w-a1", "# 人工修订标题\n\n要点已按原文校正。")

    assert out["card_id"] == "w-a1"
    assert out["edited_at"]
    card = curation.get_card(guard, "w-a1")
    assert "要点已按原文校正" in card["body"]
    assert "# 人工修订标题" in card["body"]
    assert card["status"] == "draft"  # 修订不推进状态，晋升仍是唯一放行通道
    assert card["ai_generated"] is True
    assert card["edited_at"] == out["edited_at"]
    # 列表摘要同步带出 edited_at（UI 徽标依赖）
    assert curation.list_cards(guard)[0]["edited_at"] == out["edited_at"]


def test_edit_rejects_non_draft_and_empty_body(cfg, guard):
    _make_card(guard, "w-a1", ["seed0001"])
    _make_card(guard, "w-b2", ["seed0002"], status="promoted")

    import pytest

    # promoted 是索引/图谱引用源：改它须先打回重走审核
    with pytest.raises(curation.CardEditError):
        curation.edit_card(guard, "w-b2", "改 promoted 卡")
    with pytest.raises(curation.CardEditError):
        curation.edit_card(guard, "w-a1", "   ")  # 清空正文等于毁掉审核对象
    with pytest.raises(KeyError):
        curation.edit_card(guard, "w-nope", "x")

    # 拒绝路径不得改动任何文件
    assert "要点内容" in curation.get_card(guard, "w-a1")["body"]


def test_enrich_logs_aggregation(cfg, guard):
    _seed_note(guard, "seed0001")
    _seed_note(guard, "seed0002")
    for entry_id in ("seed0001", "seed0002"):
        rel = f"sources/web/2026/{entry_id}/meta.json"
        meta = json.loads(guard.kb_root.joinpath(*rel.split("/")).read_text("utf-8"))
        meta["enrich"]["log"] = [
            {"at": f"2026-10-03T10:0{1 if entry_id.endswith('1') else 2}:00+08:00", "outcome": "enriched", "attempts": 1}
        ]
        guard.write_json("enrich", rel, meta)

    logs = curation.enrich_logs(guard)
    assert len(logs) == 2
    assert logs[0]["entry_id"] == "seed0002"  # 按时间倒序
    assert logs[0]["title"] == "t-seed0002"
    assert logs[0]["outcome"] == "enriched"
    assert logs[0]["rel"] == "sources/web/2026/seed0002/note.md"
