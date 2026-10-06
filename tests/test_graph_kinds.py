"""知识图谱三视图（§5.1 ④ v0.68）回归用例。

覆盖：`kind=entity` 默认口径不变（概念卡不入实体图）、`kind=concept` 概念—实体
共现派生的阈值与 weight、`kind=mixed` 两类边并存、概念标题解析（正文首行 / 无标题
回落卡 id）、draft 概念卡不入图、非法 kind → ValueError（API 转 400）。
"""

import pytest

from fastapi.testclient import TestClient

from kbserver.app import create_app
from kbserver.indexer import Indexer

from .test_indexer import make_concept_card, make_entity_card


def _build(cfg, guard):
    """两张概念卡（其一 draft）+ 三张实体卡（sources 可控），返回 (indexer, ids)。"""
    alpha = make_entity_card(guard, "Alpha", sources=["s1", "s2"])
    ids = {
        "alpha": alpha,
        # 与概念只共享 1 条来源 → 低于阈值不连边
        "beta": make_entity_card(guard, "Beta", sources=["s1"]),
        # 关系建在 Gamma→Alpha 上（relations 事实源，kg_edges 派生用）
        "gamma": make_entity_card(
            guard,
            "Gamma",
            sources=["s1", "s2", "s3"],
            relations=[{"type": "依赖", "target": alpha, "name": "Alpha"}],
        ),
    }
    ids["concept"] = make_concept_card(guard, "概念甲", sources=["s1", "s2"])
    make_concept_card(guard, "概念乙", status="draft", sources=["s1", "s2"])  # draft 不入图
    idx = Indexer(cfg, guard)
    idx.sync()
    return idx, ids


def test_entity_kind_is_default(cfg, guard):
    """默认 entity 口径不变：节点只有 promoted 实体卡，边只有 relations。"""
    idx, _ = _build(cfg, guard)
    g = idx.graph()

    assert g["kind"] == "entity"
    assert {n["name"] for n in g["nodes"]} == {"Alpha", "Beta", "Gamma"}  # 概念卡不入实体图
    assert all(n["kind"] == "entity" for n in g["nodes"])
    assert [(e["type"], e["kind"]) for e in g["edges"]] == [("依赖", "relation")]


def test_concept_kind_cooccurrence_threshold(cfg, guard):
    """concept 视图：概念—实体共现边，共享来源 ≥ 2 才连，weight = 共享条目数。"""
    idx, ids = _build(cfg, guard)
    g = idx.graph("concept")

    assert g["kind"] == "concept"
    names = {n["name"] for n in g["nodes"]}
    assert "概念甲" in names and "概念乙" not in names  # draft 概念卡不入图
    # Alpha(s1,s2)、Gamma(s1,s2,s3) 与概念甲共享 2 条 → 连边；Beta(s1) 共享 1 条 → 不连
    assert sorted((e["target"], e["weight"]) for e in g["edges"]) == sorted(
        [(ids["alpha"], 2), (ids["gamma"], 2)]
    )
    assert all(e["source"] == ids["concept"] for e in g["edges"])
    assert all(e["type"] == "共现" and e["kind"] == "cooccurrence" for e in g["edges"])
    concept = next(n for n in g["nodes"] if n["kind"] == "concept")
    assert concept["entity_type"] == "概念" and concept["sources"] == 2


def test_mixed_kind_merges_cooccurrence_and_relations(cfg, guard):
    """mixed 视图：共现边与实体 relations 并存，靠 edge.kind 区分。"""
    idx, ids = _build(cfg, guard)
    g = idx.graph("mixed")

    rel = [e for e in g["edges"] if e["kind"] == "relation"]
    co = [e for e in g["edges"] if e["kind"] == "cooccurrence"]
    assert [(e["source"], e["target"], e["type"]) for e in rel] == [
        (ids["gamma"], ids["alpha"], "依赖")
    ]
    assert len(co) == 2
    assert g["total_nodes"] == 4 and g["total_edges"] == 3


def test_concept_title_parsed_from_body(cfg, guard):
    """概念卡无 title 字段：名称从正文首行解析，无标题回落卡 id。"""
    c1 = make_concept_card(guard, "概念甲", heading="列式存储概览\n")
    c2 = make_concept_card(guard, "概念乙", heading="")
    idx = Indexer(cfg, guard)
    idx.sync()
    g = idx.graph("concept")

    names = {n["id"]: n["name"] for n in g["nodes"]}
    assert names[c1] == "列式存储概览"  # 正文直接开头（无 # 前缀）也取该行
    assert names[c2] == c2  # 全空正文 → 回落卡 id


def test_unknown_kind_rejected(cfg, guard):
    """非法 kind：Indexer 抛 ValueError，API 转 400（不触发索引同步）。"""
    idx, _ = _build(cfg, guard)
    with pytest.raises(ValueError):
        idx.graph("bogus")

    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/api/graph?kind=bogus").status_code == 400
        assert client.get("/api/graph").json()["kind"] == "entity"  # 默认兼容既有消费方
        assert client.get("/api/graph?kind=concept").json()["kind"] == "concept"
