"""AI 整理引擎（§11.4 实习生）：normalized 条目 → wiki 卡草稿 + 白名单字段回写。

任务分级（§9 决策 4 / §5.1 enrich 实施口径）：
- tags：打标签（便宜快模型）→ frontmatter `tags`；
- summary_card：摘要卡（强模型）→ `wiki/` draft 卡，摘要同时回写 frontmatter `ai.*`；
- concept_card：任务槽预留，不自动生成。

写边界：卡片产物只写 `wiki/`；对源条目仅经守卫补丁通道写 `tags`/`ai` 两字段
（status 由编排器以独立的 state 补丁通道维护）。失败抛 EnrichError，
由编排器决定重试或转 error；产出全部带 `ai_generated: true` 溯源。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

import yaml

from .config import Config
from .frontmatter import split_note
from .guard import Guard
from .llm import LLMError, client_for_task

MAX_CONTENT_CHARS = 6000
MAX_TAGS = 8

TAGS_WHITELIST = {"tags", "ai"}
STATUS_WHITELIST = {"status"}

_TAGS_PROMPT = (
    "你是知识库整理助手。请为下面的内容打标签：3~{max_tags} 个、每个不超过 8 个字、"
    "小写或中文短语，反映主题而非情绪。只输出 JSON 数组，不要输出其他文字。\n\n"
    "标题：{title}\n\n内容：\n{content}"
)

_CARD_PROMPT = (
    "你是知识库整理助手。请阅读下面的内容，生成一张摘要卡。\n"
    '只输出一个 JSON 对象：{{"summary": "一两句话的条目摘要", "card": "wiki 卡正文（Markdown）"}}。\n'
    "card 要求：不要输出一级标题（题目会另加），以二级标题分节（如：要点/细节，可按内容增删），"
    "忠实原文、不要编造，控制在 400 字以内。\n\n标题：{title}\n\n内容：\n{content}"
)


class EnrichError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_json_block(text: str):
    """从 LLM 回复中提取第一个 JSON 值（容忍代码围栏与前后缀文本）。"""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start == -1:
            continue
        end = cleaned.rfind(closer)
        if end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except ValueError:
                continue
    raise EnrichError(f"model returned non-JSON reply: {text[:120]}")


def wiki_card_id(entry_id: str, card_type: str = "summary") -> str:
    """确定性 wiki 卡 id（§4.5）：w- + SHA-1(源条目id:type) 前 12 位，幂等覆盖。"""
    return "w-" + hashlib.sha1(f"{entry_id}:{card_type}".encode("utf-8")).hexdigest()[:12]


def _normalize_tags(raw) -> list[str]:
    if not isinstance(raw, list):
        raise EnrichError("tags reply is not a JSON array")
    tags = []
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.strip().strip("#")
        if t and t not in tags:
            tags.append(t)
    return tags[:MAX_TAGS]


class Enricher:
    def __init__(self, cfg: Config, guard: Guard, client_factory=None):
        self.cfg = cfg
        self.guard = guard
        self._client_factory = client_factory or (
            lambda task: client_for_task(cfg.data.get("ai", {}), task)
        )

    def _chat(self, task: str, prompt: str) -> str:
        client = self._client_factory(task)
        try:
            return client.chat([{"role": "user", "content": prompt}])
        except LLMError as exc:
            raise EnrichError(str(exc)) from exc

    def enrich_entry(self, note_rel: str) -> dict:
        path = self.guard.kb_root.joinpath(*note_rel.split("/"))
        fm, body = split_note(path.read_text(encoding="utf-8"))
        entry_id = str(fm.get("id") or "")
        if not entry_id:
            raise EnrichError(f"missing id in frontmatter: {note_rel}")
        title = str(fm.get("title") or entry_id)
        content = body.strip()[:MAX_CONTENT_CHARS]
        if not content:
            raise EnrichError(f"empty note body: {note_rel}")

        # 1) 标签（便宜快模型）
        tags_raw = self._chat("tags", _TAGS_PROMPT.format(max_tags=MAX_TAGS, title=title, content=content))
        tags = _normalize_tags(_parse_json_block(tags_raw))

        # 2) 摘要卡（强模型）
        card_raw = self._chat("summary_card", _CARD_PROMPT.format(title=title, content=content))
        card_obj = _parse_json_block(card_raw)
        if not isinstance(card_obj, dict) or not str(card_obj.get("summary", "")).strip():
            raise EnrichError("summary reply missing 'summary' field")
        summary = str(card_obj["summary"]).strip()
        card_body = str(card_obj.get("card") or "").strip() or summary

        model = str(self.cfg.data.get("ai", {}).get("tasks", {}).get("summary_card", {}).get("model") or "")
        if not model:
            provider = self.cfg.data.get("ai", {}).get("tasks", {}).get("summary_card", {}).get("provider", "")
            model = str(self.cfg.data.get("ai", {}).get("providers", {}).get(provider, {}).get("model", ""))

        # 3) wiki 摘要卡：draft 态写隔离区（幂等覆盖，永不自动晋升）
        card_id = wiki_card_id(entry_id, "summary")
        card_rel = f"wiki/{card_id}.md"
        card_fm = {
            "id": card_id,
            "type": "summary",
            "ai_generated": True,
            "status": "draft",
            "sources": [entry_id],
            "created_at": _now_iso(),
            "model": model,
        }
        # 模型若自带一级标题则不再重复加（实测 GLM 会输出 # 题目，导致双标题）
        first_line = card_body.lstrip().splitlines()[0] if card_body.strip() else ""
        title_heading = "" if first_line.startswith("# ") else f"# {title}\n\n"
        card_text = (
            "---\n"
            + yaml.safe_dump(card_fm, allow_unicode=True, sort_keys=False)
            + "---\n\n"
            + f"{title_heading}{card_body}\n\n> 来源：[{entry_id}]"
        )
        self.guard.write_text("enrich", card_rel, card_text)

        # 4) 回写源条目：仅 tags / ai 两字段（字段级白名单，经守卫）
        updated_fm = self.guard.patch_note_fields(
            "enrich",
            note_rel,
            {"tags": tags, "ai": {"summary": summary, "model": model, "generated_at": _now_iso()}},
            TAGS_WHITELIST,
        )

        return {
            "entry_id": entry_id,
            "entry_rel": note_rel,
            "outcome": "enriched",
            "wiki_card": card_rel,
            "tags": updated_fm.get("tags", []),
        }
