"""问答管道测试（v0.61）：plan_ask 混合检索/实体锚定/降级 + build_messages + API 流式端点。"""

import json

import pytest
from fastapi.testclient import TestClient

from kbserver import ask as askmod
from kbserver.app import create_app
from kbserver.indexer import SemanticSearchError
from kbserver.orchestrator import Orchestrator

from .conftest import make_fetcher


def _hit(rel, rank_hint=""):
    return {
        "rel": rel,
        "kind": "entry",
        "entry_id": rel,
        "title": f"标题 {rel}",
        "url": f"https://example.com/{rel}",
        "date": "",
        "tags": [],
        "status": "enriched",
        "snippet": f"片段 {rel} {rank_hint}",
    }


class StubIndexer:
    """duck-typing 索引桩：固定检索结果与实体卡，覆盖 plan_ask 的融合/降级/锚定口径。"""

    def __init__(self, ft=None, sem=None, docs=None, cards=None, vector_enabled=False):
        self.ft = ft or []
        self.sem = sem  # None = 向量通道不可用（抛 SemanticSearchError）
        self.docs = docs or {}
        self.cards = cards or []
        self.vector_enabled = vector_enabled

    def search(self, q, limit=20):
        return {"query": q, "results": self.ft}

    def search_semantic(self, q, limit=20):
        if self.sem is None:
            raise SemanticSearchError("vector disabled")
        return {"query": q, "mode": "semantic", "results": self.sem}

    def get_doc(self, rel):
        return self.docs.get(rel)

    def promoted_entity_cards(self):
        return self.cards


def _doc(rel):
    return {
        "rel": rel,
        "kind": "entry",
        "entry_id": rel,
        "title": f"标题 {rel}",
        "url": f"https://example.com/{rel}",
        "date": "",
        "tags": [],
        "status": "enriched",
        "text_raw": f"正文内容 {rel}",
    }


def test_plan_ask_hybrid_rrf_ranks_intersection_first():
    """混合检索：两路共同命中的条目经 RRF 融合排最前；标注 retrieval=hybrid。"""
    ix = StubIndexer(
        ft=[_hit("a"), _hit("b"), _hit("c")],
        sem=[_hit("b"), _hit("d")],
        docs={"a": _doc("a"), "b": _doc("b"), "c": _doc("c"), "d": _doc("d")},
        vector_enabled=True,
    )
    plan = askmod.plan_ask(ix, "查询词")
    assert plan["retrieval"] == "hybrid"
    rels = [h["rel"] for h in plan["hits"]]
    # b 两路命中（RRF 累加）→ 第一；a/c 单路全文、d 单路向量随后
    assert rels[0] == "b"
    assert set(rels) == {"a", "b", "c", "d"}
    assert all(h["text"] for h in plan["hits"])


def test_plan_ask_degrades_to_fulltext_when_semantic_unavailable():
    """向量通道不可用（未启用/漂移/缺装）自动降级纯全文，不抛错。"""
    ix = StubIndexer(ft=[_hit("a")], sem=None, docs={"a": _doc("a")})
    plan = askmod.plan_ask(ix, "查询词")
    assert plan["retrieval"] == "fulltext"
    assert [h["rel"] for h in plan["hits"]] == ["a"]


def test_plan_ask_entity_matching_name_and_alias():
    """实体锚定：name 子串命中 + 别名 token 精确命中；relations 一跳透传。"""
    cards = [
        {
            "id": "w-entitybytehouse",
            "name": "ByteHouse",
            "aliases": ["BH"],
            "entity_type": "产品",
            "relations": [{"type": "属于", "target": "w-volc", "name": "火山引擎"}],
            "status": "promoted",
        },
        {"id": "w-other", "name": "ZooKeeper", "aliases": [], "entity_type": "组件", "relations": [], "status": "promoted"},
    ]
    ix = StubIndexer(ft=[], cards=cards)
    ents = askmod.match_entities(ix, "ByteHouse 的 bh 用法")  # name 子串 + 别名 token
    assert [e["name"] for e in ents] == ["ByteHouse"]
    assert ents[0]["matched"] == ["ByteHouse", "BH"]
    assert ents[0]["relations"][0]["name"] == "火山引擎"
    # 未命中的实体不出现
    assert all(e["name"] != "ZooKeeper" for e in ents)


def test_build_messages_numbered_context_and_entities():
    """上下文拼装：条目带 [n] 编号、实体块、空结果占位。"""
    ix = StubIndexer(
        ft=[_hit("a")],
        sem=None,
        docs={"a": _doc("a")},
        cards=[{"id": "w-x", "name": "ByteHouse", "aliases": [], "entity_type": "产品", "relations": [], "status": "promoted"}],
    )
    plan = askmod.plan_ask(ix, "ByteHouse")
    msgs = askmod.build_messages(plan)
    assert msgs[0]["role"] == "system" and msgs[1] == {"role": "user", "content": "ByteHouse"}
    sys_text = msgs[0]["content"]
    assert "ByteHouse" in sys_text and "实体" in sys_text
    assert "[1] 标题 a" in sys_text
    # 空结果占位：不喂空上下文
    empty = askmod.build_messages({"query": "q", "hits": [], "entities": []})
    assert "没有检索到相关内容" in empty[0]["content"]


# ---------- API 端点级（真实 Indexer 空库 + 流式 stub） ----------

class StubAskLLM:
    model = "fake-ask-model"

    def chat_stream(self, messages, temperature=0.2):
        yield "依据 [1] 的 "
        yield "流式答案"
        if getattr(self.outer, "boom", False):
            raise RuntimeError("worker 炸了")


class FailingResolveLLM:
    """client_for_task 即抛（任务未配置/后端不可达）→ 502。"""

    model = ""

    def __init__(self, *a, **k):
        raise askmod.LLMError("task 'summary_card' has no provider configured")


def _parse_sse(text):
    # 注意与 test_events 的区别：ask 流是**有限流**（done/error 帧后生成器结束），
    # TestClient 的 ASGI transport 虽缓冲整个响应体，但等流结束即返回——
    # 可以整体 resp.text 后离线分帧断言；无限流 SSE（/api/events）才需要
    # asyncio 直驱 sse_generator（见 test_events.py 注释的死锁坑）。
    frames = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        etype, data = "message", None
        for line in frame.split("\n"):
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        frames.append((etype, data))
    return frames


@pytest.fixture
def client(cfg, guard, monkeypatch):
    cfg.data["ai"]["enabled"] = True
    cfg.data["pipeline"]["worker_enabled"] = False
    # 落一条语料触发 kb_root 目录创建（Indexer 需要可写的库目录打开 index.db）
    import yaml

    fm = {"id": "e1", "title": "标题 a", "url": "https://example.com/a", "platform": "web",
          "source_type": "webpage", "status": "enriched", "captured_at": "2026-10-02T10:00:00+08:00"}
    guard.write_text("normalize", "sources/a/note.md",
                     "---\n" + yaml.safe_dump(fm, allow_unicode=True) + "---\n\n正文内容 a\n")
    app = create_app(cfg, orchestrator=Orchestrator(cfg, guard, fetcher=make_fetcher(pages={}), enricher=None))
    return TestClient(app)


def test_ask_stream_frames_order_and_content(cfg, guard, monkeypatch, client):
    stub = StubAskLLM()
    stub.outer = stub
    monkeypatch.setattr(askmod, "client_for_task", lambda ai_cfg, task: stub)
    resp = client.post("/api/ask", json={"q": "测试问题"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(resp.text)
    kinds = [k for k, _ in frames]
    assert kinds[0] == "ask.meta" and kinds[-1] == "ask.done"
    assert all(k in ("ask.meta", "ask.delta", "ask.done") for k in kinds)
    meta = frames[0][1]
    assert meta["retrieval"] == "fulltext" and meta["refs"] == [] and meta["entities"] == []
    answer = "".join(d["delta"] for k, d in frames if k == "ask.delta")
    assert answer == "依据 [1] 的 流式答案"
    assert frames[-1][1]["model"] == "fake-ask-model" and frames[-1][1]["elapsed_ms"] >= 0


def test_ask_error_frame_when_worker_raises(cfg, guard, monkeypatch, client):
    """生成中途异常 → ask.error 终帧（200 流内收口），不裸 500 不静默断流。"""

    class BoomLLM(StubAskLLM):
        boom = True

    stub = BoomLLM()
    stub.outer = stub
    monkeypatch.setattr(askmod, "client_for_task", lambda ai_cfg, task: stub)
    resp = client.post("/api/ask", json={"q": "测试问题"})
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert frames[-1][0] == "ask.error"
    assert "worker 炸了" in frames[-1][1]["error"]


def test_ask_402_when_llm_unresolvable(cfg, guard, monkeypatch, client):
    """任务/后端解析失败 → 502（对齐 enrich 上游失败口径）。"""
    monkeypatch.setattr(askmod, "client_for_task", lambda ai_cfg, task: FailingResolveLLM())
    resp = client.post("/api/ask", json={"q": "测试问题"})
    assert resp.status_code == 502
    assert "no provider configured" in resp.json()["detail"]


def test_ask_gated_by_ai_enabled(cfg, guard):
    """ai.enabled 总闸关 → 409（对齐 preview 口径；不受 trigger_mode 限制）。"""
    cfg.data["ai"]["enabled"] = False
    app = create_app(cfg, orchestrator=Orchestrator(cfg, guard, fetcher=make_fetcher(pages={}), enricher=None))
    with TestClient(app) as c:
        resp = c.post("/api/ask", json={"q": "测试问题"})
        assert resp.status_code == 409


def test_ask_empty_query_400(cfg, guard, monkeypatch, client):
    monkeypatch.setattr(askmod, "client_for_task", lambda ai_cfg, task: StubAskLLM())
    resp = client.post("/api/ask", json={"q": "   "})
    assert resp.status_code == 400
