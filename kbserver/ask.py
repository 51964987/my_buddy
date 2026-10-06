"""问答管道（§5.1 ask 实施口径 v0.61）：只读消费端——检索 + 实体锚定 + 流式生成。

零落盘：不写 wiki/、不改 frontmatter、不计重试——问答是纯消费，与 enrich（生产）
本质不同，因此无写边界通道变更。三段拆分：

- plan_ask()：混合检索（FTS5 + 向量 RRF 融合）+ promoted 实体卡锚定，零副作用；
- build_messages()：上下文拼装（条目带 [n] 编号 + 实体块），与生成共用同一产物；
- ask_stream()：经 summary_card 任务槽模型流式生成（yield 文本增量）。

plan 与 stream 共用 build_messages 产物，保证"引用清单"与"喂给模型的上下文"同源
（对齐 enrich 的 plan/enrich 拆分纪律：预览即所得）。
"""

from __future__ import annotations

import logging
import time

from .indexer import Indexer, SemanticSearchError, segment
from .llm import LLMError, client_for_task

logger = logging.getLogger(__name__)

# 问答检索取 top-K 命中（混合融合后截断；个人库规模 6 条足够覆盖）
ASK_TOP_K = 6
# 每条命中入 prompt 的正文截断（控 token 成本；snippet 太短，答案质量靠正文段）
ASK_CONTEXT_CHARS = 1200
# RRF 常数（业界惯例 60）：rank 越靠前贡献越大
RRF_K = 60

# 问答系统 prompt：要求引用编号 + 无据明说（可溯源纪律，§5.1 ask 口径 ⑤）
_ASK_SYSTEM = (
    "你是个人知识库的问答助手。仅依据下方提供的【知识库资料】回答用户问题，"
    "回答中引用资料时在句末标注对应编号如 [1]、[2]（编号对应资料条目）；"
    "资料不足以回答时明确说明\"知识库中没有相关内容\"，不要编造。回答用简体中文，"
    "简洁直接，先给结论再给依据。"
)


class AskError(Exception):
    """问答管道失败（检索索引/模型调用），由端点收口为 HTTP 错误。"""


def plan_ask(indexer: Indexer, q: str, k: int = ASK_TOP_K) -> dict:
    """检索 + 实体锚定（零副作用）。

    混合口径：FTS5 全文与向量语义各取 top-K，经 RRF 融合去重后取前 k；
    向量通道任何失败（未启用/口径漂移/依赖缺装/嵌入调用失败）都自动降级
    纯 FTS5 并在返回值 `retrieval` 标注——降级不是失败，问答不因向量不可用而失败。
    """
    q = (q or "").strip()
    if not q:
        raise AskError("empty query")

    t0 = time.monotonic()
    ft = indexer.search(q, limit=k)  # 内部 ensure_fresh；sqlite3.Error 由端点收口 503
    semantic: list[dict] = []
    if indexer.vector_enabled:
        try:
            semantic = indexer.search_semantic(q, limit=k)["results"]
        except (SemanticSearchError, LLMError):
            pass  # 向量通道不可用 → 降级 fulltext（meta 帧标注）
    retrieval = "hybrid" if semantic else "fulltext"

    # RRF 融合：rel → (best_hit, score)
    fused: dict[str, dict] = {}
    for rank, h in enumerate(ft["results"]):
        item = fused.setdefault(h["rel"], {"hit": h, "score": 0.0})
        item["score"] += 1.0 / (RRF_K + rank + 1)
    for rank, h in enumerate(semantic):
        item = fused.setdefault(h["rel"], {"hit": h, "score": 0.0})
        item["score"] += 1.0 / (RRF_K + rank + 1)
    ranked = sorted(fused.values(), key=lambda x: x["score"], reverse=True)[:k]

    hits = []
    for item in ranked:
        h = item["hit"]
        doc = indexer.get_doc(h["rel"])
        if doc is None:
            continue  # 索引与语料偶发不同步：跳过该条
        hits.append(
            {
                "rel": doc["rel"],
                "kind": doc["kind"],
                "entry_id": doc["entry_id"],
                "title": doc["title"] or doc["entry_id"],
                "url": doc["url"],
                "status": doc["status"],
                "snippet": h.get("snippet", ""),
                "text": doc["text_raw"][:ASK_CONTEXT_CHARS],
            }
        )

    plan = {
        "query": q,
        "retrieval": retrieval,
        "hits": hits,
        "entities": match_entities(indexer, q),
    }
    # v0.62：每次问答一条 INFO（retrieval/是否降级/耗时/引用数）；不记 query 全文与生成内容
    logger.info(
        "问答检索完成：retrieval=%s，引用 %d 条，实体 %d 个，耗时 %dms",
        retrieval, len(hits), len(plan["entities"]), elapsed_ms(t0),
    )
    return plan


def match_entities(indexer: Indexer, q: str) -> list[dict]:
    """promoted 实体卡锚定（GraphRAG local search 最小形态）。

    匹配口径：查询小写子串命中 name/aliases，或 jieba token 精确等于 name/aliases
    （小写比较）。卡量级小（几十），内存匹配即可，无新增索引。
    """
    qn = q.lower()
    tokens = {t for t in segment(q)}
    out = []
    for fm in indexer.promoted_entity_cards():
        name = str(fm.get("name") or "").strip()
        if not name:
            continue
        aliases = [str(a).strip() for a in (fm.get("aliases") or []) if str(a).strip()]
        names = [name] + aliases
        matched = [
            n for n in names
            if n.lower() in qn or n.lower() in tokens
        ]
        if not matched:
            continue
        # 一跳关系（target 名称经卡 id 解析：relations 只有 target id，展示层补名字）
        out.append(
            {
                "id": str(fm.get("id")),
                "name": name,
                "matched": matched,
                "entity_type": str(fm.get("entity_type") or ""),
                "aliases": aliases,
                "relations": [
                    {"type": str(r.get("type") or ""), "target": str(r.get("target") or ""), "name": str(r.get("name") or "")}
                    for r in (fm.get("relations") or [])
                    if isinstance(r, dict) and r.get("target")
                ],
            }
        )
    return out


def build_refs(plan: dict) -> list[dict]:
    """引用清单（ask.meta 帧与前端展示同源）：不含正文，只含定位字段。"""
    return [
        {
            "n": i + 1,
            "rel": h["rel"],
            "kind": h["kind"],
            "entry_id": h["entry_id"],
            "title": h["title"],
            "url": h["url"],
            "status": h["status"],
        }
        for i, h in enumerate(plan["hits"])
    ]


def build_messages(plan: dict) -> list[dict]:
    """上下文拼装（plan 与流式生成共用）：实体块 + 编号条目资料。"""
    parts: list[str] = []
    ents = plan["entities"]
    if ents:
        lines = []
        for e in ents:
            rels = "；".join(f"{r['type']}→{r['name'] or r['target']}" for r in e["relations"]) or "无"
            alias = f"（别名：{'、'.join(e['aliases'])}）" if e["aliases"] else ""
            lines.append(f"- {e['name']}{alias}（类型：{e['entity_type'] or '未知'}）；关系：{rels}")
        parts.append("【知识图谱实体】\n" + "\n".join(lines))
    for i, h in enumerate(plan["hits"], 1):
        parts.append(f"[{i}] {h['title']}\n{h['text']}")
    context = "\n\n".join(parts) if parts else "（知识库中没有检索到相关内容）"
    return [
        {"role": "system", "content": _ASK_SYSTEM + "\n\n【知识库资料】\n" + context},
        {"role": "user", "content": plan["query"]},
    ]


def ask_stream(ai_cfg: dict, messages: list[dict]):
    """流式生成：返回 (model, 增量生成器)。任务未配置/模型不可达抛 LLMError。"""
    client = client_for_task(ai_cfg, "summary_card")
    return client.model, client.chat_stream(messages)


def elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
