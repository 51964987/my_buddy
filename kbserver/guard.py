"""写边界守卫（§11.4 门禁系统）：所有落盘的唯一通道。

区域白名单 + 路径校验 + 原子写（tmp + rename）；越界拒绝并计数。
enrich 阶段字段级白名单：对源条目 frontmatter 只允许补丁式更新
（patch 键必须 ⊆ 调用方显式声明的 field_whitelist），见 patch_note_fields。

区域白名单按**操作分域**（§4.4 v0.34 实施口径）——两张表：
- `STAGE_REGIONS`：普通写（新建/覆盖、字段级补丁、meta.json 写）；
- `STAGE_DESTRUCTIVE_REGIONS`：破坏性写（物理删除、整树移动），**不含
  `collections/`**——D 类是源站镜像，可写不可删（v0.20 拍板），打回重生成
  （复位源条目 status）走普通写通道，物理删除仍被挡住。

v0.16 引入 D 类页时只给 normalize 放行了 collections/，enrich 未同步，导致
D 类页整理必然 500（回写被拒 → 失败流水又要写同一路径 → 冒泡）。新增区域时
必须核对"该 stage 的合法目标集"，两表都要看。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from . import events
from .frontmatter import split_note

REGION_INBOX = "inbox"
REGION_SOURCES = "sources"
REGION_COLLECTIONS = "collections"
REGION_WIKI = "wiki"

STAGE_REGIONS: dict[str, set[str]] = {
    "capture": {REGION_INBOX},
    "normalize": {REGION_SOURCES, REGION_COLLECTIONS, REGION_INBOX},
    # 源条目含 A/B/C 类（sources/）与 D 类（collections/<id>/docs/**），
    # 两者都是 enrich 的合法目标（§5.1「D 类变更页同流程」）
    "enrich": {REGION_WIKI, REGION_SOURCES, REGION_COLLECTIONS},
    "sync": {REGION_COLLECTIONS},
    "index": {"index.db"},
    # 人工处置通道（§4.4 v0.17）：审核台三处置（晋升/打回/删除）经 API 走守卫；
    # v0.21 扩展至 inbox（丢弃通道：DELETE /api/inbox/{id} 物理删除废投递）；
    # v0.34 扩展至 collections —— 打回要复位 D 类源条目（字段级补丁 + meta.json）
    "curation": {REGION_WIKI, REGION_SOURCES, REGION_INBOX, REGION_COLLECTIONS},
    # 库分区重置（§4.4 v0.44）：POST /api/kb/reset 按区清空（四区子集，不允许全选）
    "reset": {REGION_INBOX, REGION_SOURCES, REGION_COLLECTIONS, REGION_WIKI},
    # 概念聚合（§4.4/§5.1 v0.40，v0.56 落地）：概念卡只写 wiki/ 隔离区。
    # 两张区域表核对（v0.34 教训）：不进破坏性表 → 破坏性写回落普通表仍只有
    # wiki/，聚合并无删除/移动需求，语义正确。
    "aggregate": {REGION_WIKI},
}

# 破坏性操作（物理删除/整树移动）的收窄白名单：未列出的 stage 一律沿用
# STAGE_REGIONS。collections 缺席即"镜像只可写不可删"（§4.4 v0.20 + v0.34）。
# 例外：reset（v0.44）四区全放行——重置=废弃整个镜像含注册 collection.json，
# sync 引擎不再 diff 抓回，与"镜像内单页删除会被抓回"语义不同（§4.4 实施口径）。
STAGE_DESTRUCTIVE_REGIONS: dict[str, set[str]] = {
    "curation": {REGION_WIKI, REGION_SOURCES, REGION_INBOX},
    "reset": {REGION_INBOX, REGION_SOURCES, REGION_COLLECTIONS, REGION_WIKI},
}


class WriteBoundaryError(PermissionError):
    pass


def atomic_write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            os.replace(tmp, path)
        except PermissionError:
            # Windows 偶发竞态（目标平台，§平台约束）：杀软/索引服务短暂锁定刚创建的
            # tmp 文件，os.replace 报 WinError 5 拒绝访问。有界重试吸收瞬时锁；
            # 持续失败仍抛出（真实占用不可掩盖）。
            for _ in range(4):
                time.sleep(0.05)
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    continue
            else:
                raise
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


class Guard:
    def __init__(self, kb_root: Path):
        self.kb_root = Path(kb_root)
        self.violations: Counter[str] = Counter()

    def _check(self, stage: str, rel: str, *, destructive: bool = False) -> Path:
        regions = STAGE_REGIONS.get(stage)
        if not regions:
            self._reject(stage, rel, f"unknown stage '{stage}'")
            raise AssertionError
        if destructive:
            regions = STAGE_DESTRUCTIVE_REGIONS.get(stage, regions)
        p = Path(rel.replace("\\", "/"))
        if not p.parts or p.is_absolute() or ".." in p.parts or p.parts[0].startswith("_skip"):
            self._reject(stage, rel, "invalid relative path")
            raise AssertionError
        if p.parts[0] not in regions:
            self._reject(stage, rel, f"region '{p.parts[0]}' not allowed for stage '{stage}'")
            raise AssertionError
        return self.kb_root.joinpath(*p.parts)

    def _reject(self, stage: str, rel: str, reason: str) -> None:
        self.violations[stage] += 1
        raise WriteBoundaryError(f"write boundary violation (stage={stage}): {reason}: {rel}")

    def write_text(self, stage: str, rel: str, text: str) -> Path:
        return self.write_bytes(stage, rel, text.encode("utf-8"))

    def write_bytes(self, stage: str, rel: str, data: bytes) -> Path:
        path = self._check(stage, rel)
        result = atomic_write(path, data)
        # v0.53 事件总线（§11.8 ⑥）：成功写盘后发变更通知（不含内容，前端重拉聚合端点）
        events.publish("kb.changed", stage=stage, op="write", path=rel)
        return result

    def write_json(self, stage: str, rel: str, obj: Any) -> Path:
        return self.write_text(stage, rel, json.dumps(obj, ensure_ascii=False, indent=2))

    def patch_note_fields(self, stage: str, rel: str, patch: dict, field_whitelist: set[str]) -> dict:
        """frontmatter 字段级补丁（§4.4 纪律 2）。

        仅允许更新 field_whitelist 声明的键；越界（试图改其他字段）拒绝并计数。
        正文 body 原样保留；写回经原子写。返回更新后的 frontmatter。
        """
        illegal = sorted(set(patch) - set(field_whitelist))
        if illegal:
            self._reject(stage, rel, f"fields not in whitelist {sorted(field_whitelist)}: {illegal}")
            raise AssertionError
        path = self._check(stage, rel)
        if not path.exists():
            self._reject(stage, rel, "note.md does not exist")
            raise AssertionError
        fm, body = split_note(path.read_text(encoding="utf-8"))
        fm.update(patch)
        text = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + body
        atomic_write(path, text.encode("utf-8"))
        # v0.53：字段级补丁同样是落盘变更，走同一事件通道
        events.publish("kb.changed", stage=stage, op="patch", path=rel)
        return fm

    def resolve(self, stage: str, rel: str) -> Path:
        """校验并解析路径（不做写盘）：供 stage 使用非守卫写工具（如 SQLite 直写
        index.db）时确认授权边界——区域校验与 write_bytes 同源，越界同样拒绝并计数。"""
        return self._check(stage, rel)

    def exists(self, rel: str) -> bool:
        return self.kb_root.joinpath(*Path(rel.replace("\\", "/")).parts).exists()

    def move_tree(self, stage: str, src_rel: str, dst_rel: str) -> None:
        src = self._check(stage, src_rel, destructive=True)
        dst = self._check(stage, dst_rel, destructive=True)
        if not src.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        # v0.53：整树移动（归一化投递 inbox→sources 等）也是落盘变更
        events.publish("kb.changed", stage=stage, op="move", path=dst_rel)

    def remove_tree(self, stage: str, rel: str) -> None:
        path = self._check(stage, rel, destructive=True)
        if path.exists():
            shutil.rmtree(path)
            # v0.53：删除同样是落盘变更（error 巡检丢弃/重置等）
            events.publish("kb.changed", stage=stage, op="remove", path=rel)

    def remove_path(self, stage: str, rel: str) -> None:
        """删除文件或目录（curation 处置用：wiki 卡是单文件，条目目录是树）。

        走破坏性通道（§4.4 v0.34）：`collections/` 不在白名单，镜像页删不掉。
        """
        path = self._check(stage, rel, destructive=True)
        if path.is_dir():
            shutil.rmtree(path)
            events.publish("kb.changed", stage=stage, op="remove", path=rel)  # v0.53
        elif path.exists():
            path.unlink()
            events.publish("kb.changed", stage=stage, op="remove", path=rel)  # v0.53
