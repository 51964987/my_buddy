"""AI 整理引擎（§11.4 实习生）：normalized 条目 → wiki 卡草稿 + 白名单字段回写。

任务分级（§9 决策 4 / §5.1 enrich 实施口径）：
- tags：打标签（便宜快模型）→ frontmatter `tags`；
- summary_card：摘要卡（强模型）→ `wiki/` draft 卡，摘要同时回写 frontmatter `ai.*`；
- entity_extraction（§9 决策 6 v0.40）：实体抽取（知识图谱）→ `wiki/` 实体卡，
  受 `ai.kg` schema 白名单约束，同名确定性合并；只写 wiki/，不回写源条目；
- concept_card（v0.56 接入）：concept 聚合（aggregate stage）→ `wiki/` 概念卡，
  输入 = 实体卡 + enriched 条目，同名确定性合并；复用本任务槽。

写边界：卡片产物只写 `wiki/`；对源条目仅经守卫补丁通道写 `tags`/`ai` 两字段
（status 由编排器以独立的 state 补丁通道维护）。失败抛 EnrichError，
由编排器决定重试或转 error；产出全部带 `ai_generated: true` 溯源。

触发与试跑（§5.1 v0.29）：本模块只管"算"，不管"谁来触发"——手动/自动与熔断
在编排器侧（Orchestrator）。`plan_entry` 是零落盘的 dry-run 入口，工作台的
「试跑（不落盘）」走它；`enrich_entry` = plan + 落盘。二者共用 prompt 与解析
代码，保证预览与真实执行结果一致。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime

import yaml

from .config import Config
from .frontmatter import split_note
from .guard import Guard
from .llm import LLMError, client_for_task, resolve_task

MAX_CONTENT_CHARS = 6000
MAX_TAGS = 8
# 单条目实体抽取上限（§5.1 v0.40）：控成本 + 防低质长尾
MAX_ENTITIES = 12
# 单轮概念聚合产出上限（§5.1 v0.56）：概念是跨来源的二级聚合，宁缺毋滥
MAX_CONCEPTS = 8

TAGS_WHITELIST = {"tags", "ai"}
STATUS_WHITELIST = {"status"}

_TAGS_PROMPT = (
    "你是知识库整理助手。请为下面的内容打标签：3~{max_tags} 个、每个不超过 8 个字、"
    "小写或中文短语，反映主题而非情绪。只输出 JSON 数组，不要输出其他文字。\n\n"
    "标题：{title}\n\n内容：\n{content}"
)

_CARD_PROMPT = (
    "你是知识库整理助手。请阅读下面的内容，生成一张摘要卡。\n"
    '只输出一个 JSON 对象：{{"summary": "一两句话的条目摘要", '
    '"confidence": 0到1之间的小数表示你对摘要忠实原文的把握程度, '
    '"card": "wiki 卡正文（Markdown）"}}。\n'
    "card 要求：不要输出一级标题（题目会另加），以二级标题分节（如：要点/细节，可按内容增删），"
    "忠实原文、不要编造，控制在 400 字以内。\n\n标题：{title}\n\n内容：\n{content}"
)

# 实体抽取（§9 决策 6 v0.40）：受控 schema——类型/关系都必须落在白名单内，
# 防实体爆炸与幻觉（GraphRAG/Neo4j Graph Builder 的共识做法）。
# v0.54 强化：小模型会把 JSON 示例里的占位词当实体复读（实测 minicpm5 产出
# 「产品名/人物名/地点名」等 8 张模板卡），prompt 明确禁止并配合代码侧名称校验。
_ENTITY_PROMPT = (
    "你是知识库抽取助手。从下面的内容中抽取实体与实体间关系。\n"
    "实体类型只能是：{entity_types}；关系类型只能是：{relation_types}。"
    "只抽文中明确出现的**具体**事物名称（如具体的产品、技术、组织、人物的名字），"
    "最多 {max_entities} 个实体；关系两端必须是抽出的实体。\n"
    "重要：占位词不是实体——「实体名」「产品名」「人物名」这类示例模板词、泛称一律不要输出；"
    "文中没有符合条件的实体就给空数组，不要编造。"
    '只输出一个 JSON 对象：{{"entities": [{{"name": "实体名", "type": "实体类型", '
    '"aliases": ["别名，可省略"]}}], "relations": [{{"source": "实体名", '
    '"type": "关系类型", "target": "实体名"}}]}}，不要输出其他文字。\n\n'
    "标题：{title}\n\n内容：\n{content}"
)

# 实体名质量黑名单（v0.54 机械防线）：常见占位词/泛称，即使模型复读也不入库。
# 用精确集合而非子串匹配，避免误伤真实实体（如「产品名单」不受影响——该防线只作用于实体名）。
_TEMPLATE_ENTITY_NAMES = frozenset(
    {
        "实体名", "产品名", "人物名", "人名", "地点名", "地名", "位置名",
        "组织名", "机构名", "公司名", "团队名", "技术名", "概念名", "工具名",
        "事件名", "事件", "时间", "日期", "示例",
    }
)


def is_bad_entity_name(name: str, etype: str) -> bool:
    """实体名合理性校验（v0.54 机械防线，§5.1 实施口径）：

    模板词 / 与实体类型同名 / 长度不足 2（单字）→ 不可入库。
    模型侧有 prompt 禁令（_ENTITY_PROMPT v0.54 强化），此处是代码侧最后防线
    ——schema 强校验管类型合法性，管不住「复读示例」这类语义垃圾。
    """
    name = name.strip()
    if not name or name in _TEMPLATE_ENTITY_NAMES:
        return True
    if name == etype.strip():
        return True
    return len(name) < 2


def _is_bad_entity_alias(alias: str) -> bool:
    """别名合理性校验（v0.54）：含模板词的别名丢弃（实测小模型产出「产品名，可省略」）。

    别名允许子串匹配（模板词出现在别名里基本就是复读），实体名用精确匹配。
    """
    return any(t in alias for t in _TEMPLATE_ENTITY_NAMES)


# 概念聚合 prompt（§5.1 v0.40 ②，v0.56 落地）：输入 = 实体卡 + enriched 条目清单，
# LLM 聚类产出概念。与实体抽取同一纪律：引用只能来自给定清单、无概念给空数组
# 不编造、禁止占位词标题（配合代码侧 _is_bad_entity_name 校验）。
_CONCEPT_PROMPT = (
    "你是知识库整理助手。下面给出知识库中已抽取的实体卡清单与已整理条目清单。\n"
    "请把它们聚类成若干「概念」：概念是对一组相关实体/条目的主题概括（如某项技术、某个领域），"
    "要综合多个来源，不是对单个实体的复述。\n"
    "约束：最多 {max_concepts} 个概念；entity_refs（实体名）与 entry_refs（条目id）只能从给定清单中选，"
    "两者至少填一个；没有合适的概念就给空数组，不要编造；标题要具体（如「列式存储与 MergeTree」），"
    "禁止「概念一」「主题」这类占位词。\n"
    '只输出一个 JSON 对象：{{"concepts": [{{"title": "概念标题", "summary": "两三句话概括", '
    '"card": "wiki 卡正文（Markdown，不要一级标题，400 字以内）", "entity_refs": ["实体名"], '
    '"entry_refs": ["条目id"], "confidence": 0到1之间的小数}}]}}，不要输出其他文字。\n\n'
    "实体卡清单（名称 | 类型 | 别名 | 来源条目数）：\n{entities}\n\n"
    "条目清单（id | 标题 | AI摘要）：\n{entries}"
)


class EnrichError(Exception):
    pass


class EntryNotFound(EnrichError):
    """目标条目不存在（sources + collections 两区都按 frontmatter id 找不到）。"""


class EntryStateError(EnrichError):
    """条目状态不允许 enrich（仅 normalized / error 可加工）。"""


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


def concept_card_id(title: str) -> str:
    """概念卡确定性 id（§4.5 v0.40，v0.56 落地）：w- + SHA-1(concept:标题小写) 前 12 位。

    同名即同卡：重跑幂等覆盖合并，与实体卡同一确定性根基。
    """
    return "w-" + hashlib.sha1(f"concept:{title.strip().lower()}".encode("utf-8")).hexdigest()[:12]


def wiki_card_id(entry_id: str, card_type: str = "summary") -> str:
    """确定性 wiki 卡 id（§4.5）：w- + SHA-1(源条目id:type) 前 12 位，幂等覆盖。"""
    return "w-" + hashlib.sha1(f"{entry_id}:{card_type}".encode("utf-8")).hexdigest()[:12]


def entity_card_id(name: str) -> str:
    """实体卡确定性 id（§4.5 v0.40）：w- + SHA-1(entity:规范名小写) 前 12 位。

    同名即同卡——同名确定性合并的根基：不同条目抽到同一实体落到同一张卡，
    重跑幂等覆盖，首版不做模糊消歧（别名归集后置）。
    """
    return "w-" + hashlib.sha1(f"entity:{name.strip().lower()}".encode("utf-8")).hexdigest()[:12]


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


def _as_str_list(raw) -> list[str]:
    """宽容读取 frontmatter 列表字段（合并语义需要，非兜底）：非列表/含空项返回干净列表。"""
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


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

    def _extract_kg(self, title: str, content: str, kg_cfg: dict) -> dict:
        """实体抽取（§9 决策 6 v0.40）：调 LLM + 受控 schema 校验。

        白名单约束双保险：prompt 内声明 + 解析后强校验——实体类型越界丢弃、
        关系类型越界丢弃、孤儿关系（端点不在抽出实体内）丢弃、同名实体取首个、
        关系按 (type, target 卡 id) 去重。entity_types 为空 = 未定义 schema，
        无从约束抽取，跳过任务（不发起 LLM 调用）。
        """
        entity_types = [t for t in _as_str_list(kg_cfg.get("entity_types"))]
        relation_types = set(_as_str_list(kg_cfg.get("relation_types")))
        if not entity_types:
            return {"entities": [], "relations": [], "discarded": 0}
        raw = self._chat(
            "entity_extraction",
            _ENTITY_PROMPT.format(
                entity_types="、".join(entity_types),
                relation_types="、".join(sorted(relation_types)) or "（无）",
                max_entities=MAX_ENTITIES,
                title=title,
                content=content,
            ),
        )
        obj = _parse_json_block(raw)
        if not isinstance(obj, dict):
            raise EnrichError("entity reply is not a JSON object")

        entities: list[dict] = []
        by_name: dict[str, dict] = {}
        discarded = 0  # v0.54：名称质量校验丢弃数（可观测，进 plan）
        for item in obj.get("entities") or []:
            if not isinstance(item, dict) or len(entities) >= MAX_ENTITIES:
                continue
            name = str(item.get("name") or "").strip()
            etype = str(item.get("type") or "").strip()
            if not name or etype not in entity_types or name.lower() in by_name:
                continue
            if is_bad_entity_name(name, etype):
                discarded += 1
                continue
            aliases = []
            for a in _as_str_list(item.get("aliases")):
                if a and a != name and a not in aliases and not _is_bad_entity_alias(a):
                    aliases.append(a)
            ent = {"name": name, "type": etype, "aliases": aliases, "card_id": entity_card_id(name)}
            entities.append(ent)
            by_name[name.lower()] = ent

        relations: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in obj.get("relations") or []:
            if not isinstance(item, dict):
                continue
            rtype = str(item.get("type") or "").strip()
            src = by_name.get(str(item.get("source") or "").strip().lower())
            dst = by_name.get(str(item.get("target") or "").strip().lower())
            if rtype not in relation_types or not src or not dst:
                continue
            key = (src["card_id"], rtype, dst["card_id"])
            if key in seen:
                continue
            seen.add(key)
            relations.append({"source": src["name"], "type": rtype, "target": dst["name"]})
        return {"entities": entities, "relations": relations, "discarded": discarded}

    def plan_entry(self, note_rel: str) -> dict:
        """调模型 + 解析，返回结构化结果；**零落盘**（§5.1 v0.29 试跑口径）。

        试跑（dry-run）与真实执行共用本方法与同一 prompt/解析代码——预览所见即落盘所得。
        不写 `wiki/`、不改源条目 frontmatter、不计 enrich.attempts、不写 enrich.log。
        """
        started = time.perf_counter()
        path = self.guard.kb_root.joinpath(*note_rel.split("/"))
        fm, body = split_note(path.read_text(encoding="utf-8"))
        entry_id = str(fm.get("id") or "")
        if not entry_id:
            raise EnrichError(f"missing id in frontmatter: {note_rel}")
        title = str(fm.get("title") or entry_id)
        original_url = str(fm.get("url") or "").strip()
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

        # 置信度（§4.5 v0.17）：模型自评 0~1；模型未给或非法时不写字段（不造默认值）
        confidence: float | None = None
        raw_conf = card_obj.get("confidence")
        if isinstance(raw_conf, (int, float)) and 0.0 <= float(raw_conf) <= 1.0:
            confidence = round(float(raw_conf), 2)

        # 3) 实体抽取（§9 决策 6 v0.40）：受 ai.kg 白名单约束；与标签/摘要共用试跑通道
        kg_cfg = self.cfg.data.get("ai", {}).get("kg") or {}
        kg = self._extract_kg(title, content, kg_cfg)

        # 实际生效后端（试跑要展示"用的哪个模型/哪个地址"，而非配置里名义上的 provider）
        resolved = resolve_task(self.cfg.data.get("ai", {}), "summary_card")
        # 实体卡 model 字段记实体抽取任务的实际模型（与摘要卡可能不同档，§9 决策 4）
        entity_model = ""
        entity_base_url = ""
        if kg["entities"]:
            resolved_entity = resolve_task(self.cfg.data.get("ai", {}), "entity_extraction")
            entity_model = resolved_entity["model"]
            entity_base_url = resolved_entity["base_url"]
        return {
            "entry_id": entry_id,
            "title": title,
            "original_url": original_url,
            "tags": tags,
            "summary": summary,
            "card_body": card_body,
            "confidence": confidence,
            "card_id": wiki_card_id(entry_id, "summary"),
            "entities": kg["entities"],
            "relations": kg["relations"],
            "entities_discarded": kg["discarded"],
            "entity_model": entity_model,
            "entity_base_url": entity_base_url,
            "provider": str(self.cfg.data.get("ai", {}).get("tasks", {}).get("summary_card", {}).get("provider", "")),
            "model": resolved["model"],
            "base_url": resolved["base_url"],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    def enrich_entry(self, note_rel: str) -> dict:
        """plan + 落盘（试跑与执行的唯一差异就在这一步）。"""
        plan = self.plan_entry(note_rel)
        entry_id = plan["entry_id"]
        title = plan["title"]
        confidence = plan["confidence"]
        model = plan["model"]

        # 3) wiki 摘要卡：draft 态写隔离区（幂等覆盖，永不自动晋升）
        card_id = plan["card_id"]
        card_rel = f"wiki/{card_id}.md"
        card_fm = {
            "id": card_id,
            "type": "summary",
            "ai_generated": True,
            "status": "draft",
            "sources": [entry_id],
            "confidence": confidence,
            "created_at": _now_iso(),
            "model": model,
        }
        if confidence is None:
            card_fm.pop("confidence")
        card_body = plan["card_body"]
        # 模型若自带一级标题则不再重复加（实测 GLM 会输出 # 题目，导致双标题）
        first_line = card_body.lstrip().splitlines()[0] if card_body.strip() else ""
        title_heading = "" if first_line.startswith("# ") else f"# {title}\n\n"
        # 来源行必须是完整可点链接（§4.5 v0.35）：此前写成 `[id]` 残缺语法，
        # 渲染后是裸方括号，等于没有溯源；无 url 的条目降级为纯文本来源行
        url = plan.get("original_url") or ""
        source_line = (
            f"> 来源：[{entry_id}]({url})" if url else f"> 来源条目：{entry_id}"
        )
        card_text = (
            "---\n"
            + yaml.safe_dump(card_fm, allow_unicode=True, sort_keys=False)
            + "---\n\n"
            + f"{title_heading}{card_body}\n\n{source_line}"
        )
        self.guard.write_text("enrich", card_rel, card_text)

        # 3.5) 实体卡（§4.5 v0.40）：同名确定性合并写 wiki/ 隔离区
        entity_rels = self._write_entity_cards(plan, entry_id, url)

        # 4) 回写源条目：仅 tags / ai 两字段（字段级白名单，经守卫）
        ai_block = {"summary": plan["summary"], "model": model, "generated_at": _now_iso()}
        if confidence is not None:
            ai_block["confidence"] = confidence
        updated_fm = self.guard.patch_note_fields(
            "enrich",
            note_rel,
            {"tags": plan["tags"], "ai": ai_block},
            TAGS_WHITELIST,
        )

        return {
            "entry_id": entry_id,
            "entry_rel": note_rel,
            "outcome": "enriched",
            "wiki_card": card_rel,
            "entity_cards": entity_rels,
            "entities_discarded": plan.get("entities_discarded", 0),
            "tags": updated_fm.get("tags", []),
        }

    def _write_entity_cards(self, plan: dict, entry_id: str, url: str) -> list[str]:
        """实体卡落盘（§4.5 v0.40）：同名确定性合并——幂等覆盖，不自动晋升。

        合并语义：sources/aliases 并集去重；relations 按 (type, target) 去重；
        status 沿用既有值（promoted 不因重跑降级回 draft）；created_at 保留首次；
        entity_type 保留既有（schema 改名时不悄悄改历史卡）。
        """
        by_name = {e["name"].lower(): e for e in plan["entities"]}
        rels: list[str] = []
        for ent in plan["entities"]:
            card_id = ent["card_id"]
            rel = f"wiki/{card_id}.md"
            path = self.guard.kb_root.joinpath(*rel.split("/"))
            old_fm: dict = {}
            if path.exists():
                old_fm, _ = split_note(path.read_text(encoding="utf-8"))

            sources = list(dict.fromkeys([*_as_str_list(old_fm.get("sources")), entry_id]))
            aliases = list(dict.fromkeys([*_as_str_list(old_fm.get("aliases")), *ent["aliases"]]))
            # 本实体新发出的关系 → 结构化边（target = 目标实体卡 id，§4.5）
            new_rels = [
                {"type": r["type"], "target": by_name[r["target"].lower()]["card_id"], "name": r["target"]}
                for r in plan["relations"]
                if r["source"].lower() == ent["name"].lower()
            ]
            merged_rels: dict[tuple[str, str], dict] = {}
            for r in [*(old_fm.get("relations") or []), *new_rels]:
                if not isinstance(r, dict):
                    continue
                rtype = str(r.get("type") or "").strip()
                tgt = str(r.get("target") or "").strip()
                if not rtype or not tgt:
                    continue
                merged_rels.setdefault((rtype, tgt), {"type": rtype, "target": tgt, "name": str(r.get("name") or "").strip()})

            status = old_fm.get("status") if old_fm.get("status") in ("draft", "promoted") else "draft"
            fm: dict = {
                "id": card_id,
                "type": "entity",
                "ai_generated": True,
                "status": status,
                "name": ent["name"],
                "aliases": aliases,
                "entity_type": str(old_fm.get("entity_type") or ent["type"]),
                "relations": list(merged_rels.values()),
                "sources": sources,
                "confidence": plan["confidence"],
                "created_at": str(old_fm.get("created_at") or _now_iso()),
                "model": plan["entity_model"],
            }
            if fm["confidence"] is None:
                if "confidence" in old_fm:
                    fm["confidence"] = old_fm["confidence"]
                else:
                    fm.pop("confidence")

            self.guard.write_text("enrich", rel, self._render_entity_card(fm, entry_id, url))
            rels.append(rel)
        return rels

    @staticmethod
    def _render_entity_card(fm: dict, current_entry_id: str, current_url: str) -> str:
        """实体卡正文（§4.5 正文构成纪律）：模板标题 + 结构化信息 + 末尾可点来源行。

        来源行逐条列出（concept/entity 天然多来源）；仅当前条目持有 url 可写
        可点链接，历史来源降级为纯文本行（§4.5 无 url 降级口径）。
        """
        lines = [f"# {fm['name']}", "", f"- 类型：{fm['entity_type']}"]
        if fm["aliases"]:
            lines.append(f"- 别名：{'、'.join(fm['aliases'])}")
        for r in fm["relations"]:
            lines.append(f"- 关系：{r['type']} → [{r['name']}](./{r['target']}.md)")
        lines.append("")
        for sid in fm["sources"]:
            if sid == current_entry_id and current_url:
                lines.append(f"> 来源：[{sid}]({current_url})")
            else:
                lines.append(f"> 来源条目：{sid}")
        return (
            "---\n"
            + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
            + "---\n\n"
            + "\n".join(lines)
            + "\n"
        )

    # ---------- 概念聚合（§5.1 v0.40 ②，v0.56 落地：aggregate stage） ----------

    def _collect_concept_inputs(self) -> dict:
        """收集聚合输入（只读）：wiki/ 实体卡 + enriched 条目（id/title/ai.summary/url）。"""
        entities: dict[str, dict] = {}  # 实体名小写 → 卡信息
        entity_cards: list[dict] = []
        wiki = self.guard.kb_root / "wiki"
        if wiki.is_dir():
            for card in sorted(wiki.rglob("*.md")):
                try:
                    fm, _ = split_note(card.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if str(fm.get("type") or "") != "entity":
                    continue
                info = {
                    "card_id": str(fm.get("id") or ""),
                    "name": str(fm.get("name") or ""),
                    "type": str(fm.get("entity_type") or ""),
                    "aliases": _as_str_list(fm.get("aliases")),
                    "sources": _as_str_list(fm.get("sources")),
                }
                entity_cards.append(info)
                if info["name"]:
                    entities[info["name"].lower()] = info
        entries: dict[str, dict] = {}
        for region in ("sources", "collections"):
            root = self.guard.kb_root / region
            if not root.is_dir():
                continue
            for note in sorted(root.rglob("note.md")):
                try:
                    fm, _ = split_note(note.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if fm.get("status") != "enriched":
                    continue
                eid = str(fm.get("id") or "")
                if not eid:
                    continue
                ai = fm.get("ai") if isinstance(fm.get("ai"), dict) else {}
                entries[eid] = {
                    "id": eid,
                    "title": str(fm.get("title") or eid),
                    "summary": str((ai or {}).get("summary") or "")[:200],
                    "url": str(fm.get("url") or "").strip(),
                }
        return {"entities": entities, "entity_cards": entity_cards, "entries": entries}

    def plan_concept(self) -> dict:
        """概念聚合并解析，**零落盘**（与 enrich 试跑同一拆分口径，§5.1 v0.56）。

        `concept_card` 任务未配置 provider = 聚合整体跳过（返回 skipped 标记，
        不计熔断——未配置是用户选择，不是故障）。
        """
        started = time.perf_counter()
        task_cfg = (self.cfg.data.get("ai", {}).get("tasks") or {}).get("concept_card") or {}
        if not task_cfg.get("provider"):
            return {"skipped": "concept_card_not_configured"}

        inputs = self._collect_concept_inputs()
        if not inputs["entity_cards"] and not inputs["entries"]:
            # 无可聚合输入 = 无目标（对齐 enrich「无目标不参与熔断判定」口径）：
            # 是库状态而非故障，返回 skipped 而非抛错（避免空库时 run 穿透成 500）
            return {"skipped": "nothing_to_aggregate"}
        ent_lines = [
            f"{e['name']} | {e['type']} | {'、'.join(e['aliases']) or '—'} | {len(e['sources'])}"
            for e in inputs["entity_cards"]
        ]
        entry_lines = [
            f"{e['id']} | {e['title']} | {e['summary'] or '—'}" for e in inputs["entries"].values()
        ]
        raw = self._chat(
            "concept_card",
            _CONCEPT_PROMPT.format(
                max_concepts=MAX_CONCEPTS,
                entities="\n".join(ent_lines) or "（无）",
                entries="\n".join(entry_lines) or "（无）",
            ),
        )
        obj = _parse_json_block(raw)
        if not isinstance(obj, dict):
            raise EnrichError("concept reply is not a JSON object")

        concepts: list[dict] = []
        seen_titles: set[str] = set()
        discarded = 0
        for item in obj.get("concepts") or []:
            if not isinstance(item, dict) or len(concepts) >= MAX_CONCEPTS:
                continue
            title = str(item.get("title") or "").strip()
            if not title or is_bad_entity_name(title, "") or title.lower() in seen_titles:
                discarded += 1
                continue
            # 引用强校验：实体按名称解析到实体卡 id，条目按 id 校验存在性；
            # 无效引用丢弃（不静默编造溯源），双空引用的概念整体丢弃
            entity_ids: list[str] = []
            for name in _as_str_list(item.get("entity_refs")):
                info = inputs["entities"].get(name.lower())
                if info and info["card_id"] not in entity_ids:
                    entity_ids.append(info["card_id"])
            entry_ids: list[str] = []
            for eid in _as_str_list(item.get("entry_refs")):
                if eid in inputs["entries"] and eid not in entry_ids:
                    entry_ids.append(eid)
            if not entity_ids and not entry_ids:
                discarded += 1
                continue
            summary = str(item.get("summary") or "").strip()
            body = str(item.get("card") or "").strip() or summary
            if not body:
                discarded += 1
                continue
            confidence = None
            raw_conf = item.get("confidence")
            if isinstance(raw_conf, (int, float)) and 0.0 <= float(raw_conf) <= 1.0:
                confidence = round(float(raw_conf), 2)
            seen_titles.add(title.lower())
            concepts.append(
                {
                    "title": title,
                    "card_id": concept_card_id(title),
                    "summary": summary,
                    "card_body": body,
                    "entity_card_ids": entity_ids,
                    "entry_ids": entry_ids,
                    "confidence": confidence,
                }
            )

        resolved = resolve_task(self.cfg.data.get("ai", {}), "concept_card")
        return {
            "concepts": concepts,
            "discarded": discarded,
            "inputs": {"entities": len(inputs["entity_cards"]), "entries": len(inputs["entries"])},
            "entry_urls": {eid: e["url"] for eid, e in inputs["entries"].items() if e["url"]},
            "model": resolved["model"],
            "base_url": resolved["base_url"],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    def run_concept(self, plan: dict) -> dict:
        """概念卡落盘（守卫 stage `aggregate`，只写 wiki/）：幂等覆盖合并。

        合并语义同实体卡：status 沿用既有（promoted 不降级）、sources 并集去重
        （本轮 entry_refs + 被引实体卡的来源条目）、created_at 保留首次、confidence
        非法不写字段。试跑与执行共用 plan——差异只在落盘这一步。
        """
        written: list[dict] = []
        for c in plan["concepts"]:
            rel = f"wiki/{c['card_id']}.md"
            path = self.guard.kb_root.joinpath(*rel.split("/"))
            old_fm: dict = {}
            if path.exists():
                old_fm, _ = split_note(path.read_text(encoding="utf-8"))
            # 溯源并集：本轮条目引用 + 被引实体卡的来源条目（概念经实体间接溯源）
            sources: list[str] = list(c["entry_ids"])
            for cid in c["entity_card_ids"]:
                ent_rel = f"wiki/{cid}.md"
                ent_path = self.guard.kb_root.joinpath(*ent_rel.split("/"))
                if ent_path.exists():
                    try:
                        ent_fm, _ = split_note(ent_path.read_text(encoding="utf-8"))
                        sources.extend(_as_str_list(ent_fm.get("sources")))
                    except Exception:
                        continue
            sources = list(dict.fromkeys([*_as_str_list(old_fm.get("sources")), *sources]))
            status = old_fm.get("status") if old_fm.get("status") in ("draft", "promoted") else "draft"
            fm: dict = {
                "id": c["card_id"],
                "type": "concept",
                "ai_generated": True,
                "status": status,
                "sources": sources,
                "created_at": str(old_fm.get("created_at") or _now_iso()),
                "model": plan["model"],
            }
            if c["confidence"] is not None:
                fm["confidence"] = c["confidence"]
            elif "confidence" in old_fm:
                fm["confidence"] = old_fm["confidence"]
            body = c["card_body"]
            first_line = body.lstrip().splitlines()[0] if body.strip() else ""
            heading = "" if first_line.startswith("# ") else f"# {c['title']}\n\n"
            text = (
                "---\n"
                + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
                + "---\n\n"
                + heading
                + body
                + "\n\n"
                + self._concept_source_lines(sources, plan.get("entry_urls") or {})
            )
            self.guard.write_text("aggregate", rel, text)
            written.append(
                {"rel": rel, "id": c["card_id"], "title": c["title"], "sources": len(sources), "status": status}
            )
        return {
            "concepts": plan["concepts"],
            "written": written,
            "discarded": plan.get("discarded", 0),
            "model": plan["model"],
            "base_url": plan["base_url"],
            "elapsed_ms": plan["elapsed_ms"],
        }

    @staticmethod
    def _concept_source_lines(sources: list[str], urls: dict[str, str]) -> str:
        """概念卡来源行（§4.5 纪律）：逐条列出，有 url 写可点链接，无降级纯文本。"""
        lines = [f"> 来源：[{sid}]({urls[sid]})" if sid in urls else f"> 来源条目：{sid}" for sid in sources]
        return "\n".join(lines)
