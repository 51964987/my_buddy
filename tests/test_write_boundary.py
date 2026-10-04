"""写边界按操作分域 + D 类页可被 AI 整理（§4.4 v0.34 实施口径）。

背景（用户报障的根因）：v0.16 引入 D 类页（`collections/<id>/docs/**/note.md`）时
只给 `normalize` 放行了 `collections/`，`enrich` 的区域白名单仍是 `{wiki, sources}`，
而 enrich 的合法目标集明确含 D 类页（扫描/试跑/流水聚合都遍历两区，§5.1「D 类变更页
同流程」）——代码比设计更严的漂移。于是文档树「满意，落盘这一条」写卡成功、回写源条目
被守卫拒绝；`_enrich_fail` 记流水又要写同一个被拒路径，二次抛出无人接 → HTTP 500，
且 `error` 态与 `error_*` 全丢（半落盘）。

覆盖：
- 普通写（enrich 回写 / curation 复位）可落到 `collections/`；
- 破坏性写（物理删除）仍不含 `collections/`——v0.20「D 类镜像不删」不回归；
- 失败态落盘失败不再冒泡成 500（返回终态 `error` + 服务端日志），批量不整批中断；
- enrich 越界（写 inbox）仍被拒并计数——分域表没被误放宽。
"""

import json
import logging

import pytest
import yaml
from fastapi.testclient import TestClient

from kbserver import curation
from kbserver.app import create_app
from kbserver.enrich import wiki_card_id
from kbserver.frontmatter import split_note
from kbserver.guard import WriteBoundaryError

from .test_enrich import FakeLLM, _make_orch

COLLECTION_ID = "volcengine-bytehouse"
PAGE_DIR = "Productoverview"
COLLECTION_REL = f"collections/{COLLECTION_ID}/docs/{PAGE_DIR}"
ENTRY_ID = "5fed4d41e3cc"


def _seed_collection_page(guard, entry_id=ENTRY_ID, status="normalized", meta=None, page=PAGE_DIR):
    """造一个 D 类站点页（collections/<id>/docs/**/note.md + meta.json）。"""
    rel = f"collections/{COLLECTION_ID}/docs/{page}/note.md"
    fm = {
        "id": entry_id,
        "type": "collection_page",
        "title": "产品简介",
        "url": "https://example.com/docs/Productoverview",
        "status": status,
        "created_at": "2026-10-03T10:00:00+08:00",
    }
    guard.write_text(
        "normalize",
        rel,
        "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n正文。\n",
    )
    guard.write_json("normalize", f"collections/{COLLECTION_ID}/docs/{page}/meta.json", meta or {"content_hash": "abc"})
    return rel


def _fm(guard, rel):
    return split_note((guard.kb_root / rel).read_text(encoding="utf-8"))[0]


def _meta(guard, rel):
    return json.loads((guard.kb_root / rel.rsplit("/", 1)[0] / "meta.json").read_text(encoding="utf-8"))


def _failing_llm():
    llm = FakeLLM()
    llm.fail_tasks = {"tags"}  # 模型侧失败 → 走 _enrich_fail 分支
    return llm


# ---------- 普通写覆盖 collections/：D 类页可被整理 ----------


def test_enrich_lands_on_collection_page(cfg, guard):
    cfg.data["ai"]["enabled"] = True
    orch = _make_orch(cfg, guard, FakeLLM())
    rel = _seed_collection_page(guard)

    out = orch.enrich_run(ENTRY_ID)

    assert out["outcome"] == "enriched"
    assert dict(guard.violations) == {}  # 全程零越界
    assert guard.exists(f"wiki/{wiki_card_id(ENTRY_ID, 'summary')}.md")
    fm = _fm(guard, rel)
    assert fm["status"] == "enriched"  # 状态真的推进了（修复前卡在 normalized）
    assert fm["tags"]  # tags/ai 两字段回写成功（修复前为空）
    assert fm["ai"]["summary"]
    assert _meta(guard, rel)["enrich"]["log"][-1]["outcome"] == "enriched"


def test_enrich_api_returns_enriched_for_collection_page(cfg, guard):
    """回归用户报的 500：文档树「满意，落盘这一条」对 D 类页必须 200。"""
    cfg.data["ai"]["enabled"] = True
    orch = _make_orch(cfg, guard, FakeLLM())
    _seed_collection_page(guard)
    app = create_app(cfg, orchestrator=orch)

    with TestClient(app) as client:
        resp = client.post(f"/api/enrich/run?entry_id={ENTRY_ID}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "enriched"


def test_preview_then_run_matches_on_collection_page(cfg, guard):
    """试跑（零落盘）与落盘共用 prompt/解析：预览即所得在 D 类页同样成立。"""
    cfg.data["ai"]["enabled"] = True
    llm = FakeLLM()
    orch = _make_orch(cfg, guard, llm)
    rel = _seed_collection_page(guard)

    plan = orch.enrich_preview(ENTRY_ID)

    assert guard.exists(f"wiki/{wiki_card_id(ENTRY_ID, 'summary')}.md") is False  # 零落盘
    assert _fm(guard, rel)["status"] == "normalized"
    assert len(llm.prompts) == 3  # tags + summary_card + entity_extraction（v0.40）

    assert orch.enrich_run(ENTRY_ID)["outcome"] == "enriched"
    assert _fm(guard, rel)["tags"] == plan["tags"]


def test_enrich_batch_includes_collection_pages(cfg, guard):
    """批量扫描（D 类页在目标集内）：修复前任一 D 类页越界会把整批打断。"""
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["batch_size"] = 5
    orch = _make_orch(cfg, guard, FakeLLM())
    _seed_collection_page(guard)

    summary = orch.enrich_scan_once()

    assert summary["scanned"] == 1
    assert [r["outcome"] for r in summary["results"]] == ["enriched"]


def test_curation_regenerate_resets_collection_page(cfg, guard):
    """审核台打回：D 类源条目要能复位（打回先删 wiki 卡再复位条目，字段级补丁走普通写通道）。"""
    cfg.data["ai"]["enabled"] = True
    orch = _make_orch(cfg, guard, FakeLLM())
    rel = _seed_collection_page(guard)
    assert orch.enrich_run(ENTRY_ID)["outcome"] == "enriched"
    card_id = wiki_card_id(ENTRY_ID, "summary")

    result = curation.regenerate_card(guard, card_id)

    assert result == {"deleted": card_id, "reset_sources": [ENTRY_ID]}
    assert guard.exists(f"wiki/{card_id}.md") is False
    assert _fm(guard, rel)["status"] == "normalized"
    meta = _meta(guard, rel)
    assert meta["enrich"]["attempts"] == 0
    assert "error_stage" not in meta


# ---------- 破坏性写仍不含 collections/（v0.20 不回归） ----------


def test_collections_rejects_destructive_writes(guard):
    _seed_collection_page(guard)

    for call in (
        lambda: guard.remove_path("curation", f"{COLLECTION_REL}/note.md"),
        lambda: guard.remove_tree("curation", COLLECTION_REL),
        lambda: guard.move_tree("curation", COLLECTION_REL, f"{COLLECTION_REL}-moved"),
    ):
        with pytest.raises(WriteBoundaryError):
            call()

    assert guard.violations["curation"] == 3
    assert guard.exists(f"{COLLECTION_REL}/note.md")  # 镜像页毫发无损


def test_collection_page_delete_api_still_404(cfg, guard):
    """端点语义不变：条目删除只作用于 sources/，D 类天然 404（v0.20 选项 A）。"""
    _seed_collection_page(guard)
    app = create_app(cfg, orchestrator=_make_orch(cfg, guard, FakeLLM()))

    with TestClient(app) as client:
        resp = client.delete(f"/api/entries/{ENTRY_ID}")

    assert resp.status_code == 404
    assert guard.exists(f"{COLLECTION_REL}/note.md")


def test_curation_patches_collections_but_enrich_cannot_write_inbox(guard):
    _seed_collection_page(guard)
    # 打回复位走普通写通道：字段级补丁放行
    fm = guard.patch_note_fields(
        "curation", f"{COLLECTION_REL}/note.md", {"status": "normalized"}, {"status"}
    )
    assert fm["status"] == "normalized"
    # 越界仍被拒并计数：分域表没被整体放宽
    with pytest.raises(WriteBoundaryError):
        guard.write_text("enrich", "inbox/e1/capture.json", "{}")
    assert guard.violations["enrich"] == 1


# ---------- 失败态落盘失败不再冒泡成 500（铁律 3 可观测性底线） ----------


def test_enrich_fail_write_error_returns_error_not_500(cfg, guard, monkeypatch, caplog):
    """失败流水自身写盘失败：记服务端日志 + 返回终态 error，不冒泡（修复前直接 500）。"""
    cfg.data["ai"]["enabled"] = True
    orch = _make_orch(cfg, guard, _failing_llm())
    _seed_collection_page(guard, status="error")  # error 态条目：LLM 失败 → 进 _enrich_fail

    def boom(stage, rel_path, obj):
        raise WriteBoundaryError(f"disk gone: {rel_path}")

    monkeypatch.setattr(orch.guard, "write_json", boom)
    caplog.set_level(logging.ERROR, logger="kbserver.orchestrator")

    out = orch.enrich_run(ENTRY_ID)  # 修复前在此抛 WriteBoundaryError → HTTP 500

    assert out["outcome"] == "error"  # 不是假的 retry
    assert "失败态落盘失败" in caplog.text


def test_enrich_batch_isolates_single_entry_failure(cfg, guard, monkeypatch):
    """串行批次单条异常只影响自己（与并发路径同口径），不打断整批。"""
    cfg.data["ai"]["enabled"] = True
    cfg.data["ai"]["batch_size"] = 5
    cfg.data["ai"]["concurrency"] = 1
    orch = _make_orch(cfg, guard, FakeLLM())
    bad = _seed_collection_page(guard, entry_id="bad000000001", page="Bad")
    good = _seed_collection_page(guard, entry_id="good00000001", page="Good")

    real = orch._enrich_one

    def fake_one(rel):
        if rel != good:
            raise RuntimeError("boom")  # 单条未收口的极端情况
        return real(rel)

    monkeypatch.setattr(orch, "_enrich_one", fake_one)

    summary = orch.enrich_scan_once()

    assert summary["scanned"] == 2
    outcomes = {r["entry_id"]: r["outcome"] for r in summary["results"]}
    assert outcomes == {"bad000000001": "error", "good00000001": "enriched"}
    assert "enrich" not in guard.violations
