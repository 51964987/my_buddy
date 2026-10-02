"""写边界守卫（§11.4 门禁系统）：所有落盘的唯一通道。

区域白名单 + 路径校验 + 原子写（tmp + rename）；越界拒绝并计数。
enrich 阶段的字段级白名单（ai.*/tags）随 P3 实现。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

REGION_INBOX = "inbox"
REGION_SOURCES = "sources"
REGION_COLLECTIONS = "collections"
REGION_WIKI = "wiki"

STAGE_REGIONS: dict[str, set[str]] = {
    "capture": {REGION_INBOX},
    "normalize": {REGION_SOURCES, REGION_COLLECTIONS, REGION_INBOX},
    "enrich": {REGION_WIKI, REGION_SOURCES},
    "sync": {REGION_COLLECTIONS},
    "index": {"index.db"},
}


class WriteBoundaryError(PermissionError):
    pass


def atomic_write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


class Guard:
    def __init__(self, kb_root: Path):
        self.kb_root = Path(kb_root)
        self.violations: Counter[str] = Counter()

    def _check(self, stage: str, rel: str) -> Path:
        allowed = STAGE_REGIONS.get(stage)
        if not allowed:
            self._reject(stage, rel, f"unknown stage '{stage}'")
            raise AssertionError
        p = Path(rel.replace("\\", "/"))
        if not p.parts or p.is_absolute() or ".." in p.parts or p.parts[0].startswith("_skip"):
            self._reject(stage, rel, "invalid relative path")
            raise AssertionError
        if p.parts[0] not in allowed:
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
        return atomic_write(path, data)

    def write_json(self, stage: str, rel: str, obj: Any) -> Path:
        return self.write_text(stage, rel, json.dumps(obj, ensure_ascii=False, indent=2))

    def exists(self, rel: str) -> bool:
        return self.kb_root.joinpath(*Path(rel.replace("\\", "/")).parts).exists()

    def move_tree(self, stage: str, src_rel: str, dst_rel: str) -> None:
        src = self._check(stage, src_rel)
        dst = self._check(stage, dst_rel)
        if not src.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    def remove_tree(self, stage: str, rel: str) -> None:
        path = self._check(stage, rel)
        if path.exists():
            shutil.rmtree(path)
