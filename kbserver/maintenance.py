"""库分区重置（§4.4 v0.44 实施口径）：运维级破坏性操作的守卫通道封装。

定位：
- 物理删除 `kb/` 下指定分区（四区非空子集，**不允许全选**——全量重置含 index.db，
  进程内被 SQLite 连接持有无法删除，由 `scripts/reset_kb.ps1` 承担）；
- 一切删除经守卫 `reset` stage 破坏性通道（普通写与破坏性白名单均含四区；
  `collections/` 可重置 = 废弃整个镜像含注册 collection.json，sync 不再抓回，
  与 v0.20「单页不可删」语义不同）；
- `index.db` 不删：索引是语料镜像（铁律 1），重置后查询时懒同步 removed 通道
  自动移除已删条目；RAG 派生向量库同理不自动重嵌（涉外部费用）。
"""

from __future__ import annotations

import shutil
from datetime import datetime

from .guard import REGION_COLLECTIONS, REGION_INBOX, REGION_SOURCES, REGION_WIKI, Guard

RESET_REGIONS = (REGION_INBOX, REGION_SOURCES, REGION_COLLECTIONS, REGION_WIKI)

# 重置前整库备份排除项：index.db 及其 WAL/SHM 附属被服务进程持有，同备份脚本口径
_BACKUP_IGNORE = shutil.ignore_patterns("index.db", "index.db-wal", "index.db-shm")


def _count_files(root) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


def backup_kb(kb_root, ts: str | None = None):
    """整库备份到库同级 `kb-backup-<时间戳>`（排除 index.db），返回目标路径。"""
    ts = ts or datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = kb_root.parent / f"{kb_root.name}-backup-{ts}"
    n = 2
    while dest.exists():
        dest = kb_root.parent / f"{kb_root.name}-backup-{ts}-{n}"
        n += 1
    shutil.copytree(kb_root, dest, ignore=_BACKUP_IGNORE)
    return dest


def reset_regions(guard: Guard, regions: list[str], backup: bool = False) -> dict:
    """删除指定分区并重建空目录；返回逐区删除文件数与备份路径。"""
    counts = {r: _count_files(guard.kb_root / r) for r in regions}
    backup_path: str | None = None
    if backup:
        backup_path = str(backup_kb(guard.kb_root))
    for region in regions:
        guard.remove_tree("reset", region)
        (guard.kb_root / region).mkdir(parents=True, exist_ok=True)
    return {"reset": list(regions), "counts": counts, "backup": backup_path}
