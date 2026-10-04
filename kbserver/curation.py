"""人工处置通道（§4.4 v0.17 curation）：审核台处置 + wiki 卡/enrich 流水读取。

定位：
- 只读聚合（wiki 卡清单、enrich 操作流水）与处置指令转发，无业务裁决；
- 一切写盘经守卫 curation 阶段：wiki 卡 status 补丁、删除卡、源条目状态复位、
  修订 draft 卡正文（v0.35，整份卡原子写回）；
- 无自动晋升——本模块只由 API 人工触发（§4.5：无批量自动晋升通道）。
"""

from __future__ import annotations

import json
from datetime import datetime

import yaml

from .frontmatter import split_note
from .guard import Guard

CARD_STATUS_WHITELIST = {"status"}
SOURCE_STATUS_WHITELIST = {"status"}

ENRICH_LOG_LIMIT = 50  # 流水聚合返回上限（单条目详情见 meta.json enrich.log）


class CardEditError(Exception):
    """卡片不可修订（不存在 / 非 draft / 正文为空），由 API 层映射状态码。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


def _iter_cards(guard: Guard):
    wiki = guard.kb_root / "wiki"
    if not wiki.is_dir():
        return
    for card in sorted(wiki.rglob("*.md")):
        yield card


def _card_summary(guard: Guard, card, fm: dict, body: str) -> dict:
    return {
        "id": str(fm.get("id") or ""),
        "rel": card.relative_to(guard.kb_root).as_posix(),
        "type": str(fm.get("type") or ""),
        "ai_generated": bool(fm.get("ai_generated")),
        "status": str(fm.get("status") or ""),
        "sources": list(fm.get("sources") or []),
        "confidence": fm.get("confidence"),
        "created_at": fm.get("created_at"),
        "edited_at": fm.get("edited_at"),
        "model": fm.get("model"),
        "title": str(fm.get("title") or _first_heading(body) or fm.get("id") or ""),
    }


def list_cards(guard: Guard, status: str | None = None) -> list[dict]:
    out = []
    for card in _iter_cards(guard):
        try:
            fm, body = split_note(card.read_text(encoding="utf-8"))
        except Exception:
            continue  # 人工编辑中的半成品跳过，不阻塞清单
        if status and str(fm.get("status") or "") != status:
            continue
        out.append(_card_summary(guard, card, fm, body))
    return out


def get_card(guard: Guard, card_id: str) -> dict | None:
    for card in _iter_cards(guard):
        try:
            fm, body = split_note(card.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(fm.get("id") or "") == card_id:
            summary = _card_summary(guard, card, fm, body)
            summary["body"] = body
            return summary
    return None


def promote_card(guard: Guard, card_id: str) -> bool:
    """晋升（draft → promoted）：仅 draft 可晋升，promoted 重复晋升幂等拒绝。"""
    card = _find_card_path(guard, card_id)
    if card is None:
        return False
    rel, fm = card
    if fm.get("status") != "draft":
        return False
    guard.patch_note_fields("curation", rel, {"status": "promoted"}, CARD_STATUS_WHITELIST)
    return True


def delete_card(guard: Guard, card_id: str) -> bool:
    rel, _fm = _require_card(guard, card_id)
    guard.remove_path("curation", rel)
    return True


def edit_card(guard: Guard, card_id: str, body: str) -> dict:
    """修订 draft 卡正文（§4.5 v0.35，业界 edit-before-accept）。

    与三处置并列的第四处置，人工触发：
    - 仅 `draft` 可改：`promoted` 已是索引/图谱引用源，改动须先打回重走审核；
    - 整份卡（frontmatter + 正文）经守卫原子写回，`ai_generated: true` 与
      `status` 不变——"人工修订过"只落 `edited_at` 留痕，不篡改 AI 溯源标记；
    - 正文不得为空（清空等于毁掉审核对象）；其余 frontmatter 字段原样保留。
    """
    rel, fm = _require_card(guard, card_id)
    if str(fm.get("status") or "") != "draft":
        raise CardEditError(f"card is not draft (status={fm.get('status')}): {card_id}")
    new_body = (body or "").strip()
    if not new_body:
        raise CardEditError("card body must not be empty")
    updated = dict(fm)
    updated["edited_at"] = _now_iso()
    text = (
        "---\n"
        + yaml.safe_dump(updated, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + new_body
        + "\n"
    )
    guard.write_text("curation", rel, text)
    return {"card_id": card_id, "rel": rel, "edited_at": updated["edited_at"]}


def regenerate_card(guard: Guard, card_id: str) -> dict:
    """打回重生成：删除卡 + 源条目 status 回 normalized、重试计数清零。

    enrich worker 周期扫描会随后自动重新加工（ai.enabled 关闭时条目停在
    normalized，用户开开关或手动 /api/enrich/run 触发）。
    """
    rel, fm = _require_card(guard, card_id)
    sources = list(fm.get("sources") or [])
    guard.remove_path("curation", rel)
    reset = []
    for source_id in sources:
        note = _find_note_by_id(guard, str(source_id))
        if note is None:
            continue  # 源条目已被人工删除：卡已删即达成，不做多余动作
        note_rel, note_fm = note
        if note_fm.get("status") not in ("enriched", "error", "normalized"):
            continue
        guard.patch_note_fields(
            "curation", note_rel, {"status": "normalized"}, SOURCE_STATUS_WHITELIST
        )
        meta = _read_meta(guard, note_rel)
        enrich_state = dict(meta.get("enrich") or {})
        enrich_state["attempts"] = 0
        meta["enrich"] = enrich_state
        meta.pop("error_stage", None)
        meta.pop("error_message", None)
        guard.write_json("curation", _meta_rel(note_rel), meta)
        reset.append(str(source_id))
    return {"deleted": card_id, "reset_sources": reset}


def enrich_logs(guard: Guard, limit: int = ENRICH_LOG_LIMIT) -> list[dict]:
    """enrich 操作流水聚合（§4.4 v0.17）：扫 sources/collections 的 meta.json enrich.log。"""
    out = []
    for note_rel, note_fm in _iter_notes(guard):
        meta = _read_meta(guard, note_rel)
        for entry in (meta.get("enrich") or {}).get("log") or []:
            if not isinstance(entry, dict):
                continue
            out.append(
                {
                    "at": entry.get("at"),
                    "outcome": entry.get("outcome"),
                    "message": entry.get("message"),
                    "attempts": entry.get("attempts"),
                    "entry_id": note_fm.get("id"),
                    "title": note_fm.get("title"),
                    "rel": note_rel,
                }
            )
    out.sort(key=lambda e: e.get("at") or "", reverse=True)
    return out[: max(1, int(limit))]


# ---------------- 内部工具 ----------------


def _find_card_path(guard: Guard, card_id: str) -> tuple[str, dict] | None:
    for card in _iter_cards(guard):
        try:
            fm, _body = split_note(card.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(fm.get("id") or "") == card_id:
            return card.relative_to(guard.kb_root).as_posix(), fm
    return None


def _require_card(guard: Guard, card_id: str) -> tuple[str, dict]:
    found = _find_card_path(guard, card_id)
    if found is None:
        raise KeyError(card_id)
    return found


def _iter_notes(guard: Guard):
    for region in ("sources", "collections"):
        root = guard.kb_root / region
        if not root.is_dir():
            continue
        for note in sorted(root.rglob("note.md")):
            rel = note.relative_to(guard.kb_root).as_posix()
            try:
                fm, _body = split_note(note.read_text(encoding="utf-8"))
            except Exception:
                continue
            yield rel, fm


def _find_note_by_id(guard: Guard, entry_id: str):
    for rel, fm in _iter_notes(guard):
        if str(fm.get("id") or "") == entry_id:
            return rel, fm
    return None


def _meta_rel(note_rel: str) -> str:
    return note_rel.rsplit("/", 1)[0] + "/meta.json"


def _read_meta(guard: Guard, note_rel: str) -> dict:
    path = guard.kb_root.joinpath(*_meta_rel(note_rel).split("/"))
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
