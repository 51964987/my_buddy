# -*- coding: utf-8 -*-
"""存量库图片映射一次性回填（方案文档 §6 v0.32）。

背景：v0.32 之前 normalize 图片本地化只在 meta.json raw_files 记录落盘路径
（raw/img-<内容sha1前10位>.<ext>），原站图片 URL 被替换后未存映射——导出 merged
的 images=original 模式（图片按原始地址访问）对存量条目无映射可用。

存量数据里唯一保留原始图片 URL 的地方是 D 类页面的 raw/page.json 原件（站点 API
响应体，内含原始 MDContent）。本脚本据此回填：

    解析 raw/page.json 中的图片 URL → 逐个重抓 → sha1 对齐已落盘的 raw/img-* 文件
    → 经 normalize 守卫通道回写 meta.json raw_files（str → {"path", "src"}）

用法（默认 dry-run，只报告不写盘）：

    python scripts/migrate_image_map.py [--kb-root kb] [--collection <id>] [--apply]

注意：
- 仅处理 D 类 collection 页面（sources/ 条目无 page.json 原件，不适用）；
- 重抓失败的图片保持无映射（导出侧回落 image API URL，不影响可用性）；
- 归一化时被 max_images 截断或抓取失败而未本地化的图片，本来就以原站 URL 留在
  正文里，无需映射；
- sha1 对齐是确定性匹配（内容哈希），不依赖顺序，无误配风险。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from kbserver.guard import Guard  # noqa: E402
from kbserver.normalize import _IMAGE_EXT, _IMAGE_RE  # noqa: E402

_HEADERS = {"User-Agent": "Mozilla/5.0 (kb-buddy migrate)"}


def _find_mdcontent(obj, depth: int = 0):
    """递归查找站点 API 响应中的 MDContent 字段（volcengine 口径：Result.MDContent）。"""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        v = obj.get("MDContent")
        if isinstance(v, str) and v.strip():
            return v
        for child in obj.values():
            found = _find_mdcontent(child, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for child in obj:
            found = _find_mdcontent(child, depth + 1)
            if found:
                return found
    return None


def migrate_entry(kb_root: Path, meta_rel: Path, apply: bool) -> tuple[int, int]:
    """回填单个条目：返回 (解析出的映射数, 仍无映射的图片数)。异常向上抛由调用方计数。

    meta_rel 相对 kb_root（守卫写盘口径），形如 collections/<id>/docs/<...>/meta.json。
    """
    meta_path = kb_root / meta_rel
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    raw_files = meta.get("raw_files") or []
    unmapped = [e for e in raw_files if isinstance(e, str) and e.startswith("raw/img-")]
    if not unmapped:
        return 0, 0

    page_json = kb_root / meta_rel.parent / "raw" / "page.json"
    if not page_json.exists():
        return 0, len(unmapped)
    md = _find_mdcontent(json.loads(page_json.read_bytes()))
    if not md:
        return 0, len(unmapped)

    # 候选原站 URL（与 _localize_images 同口径：仅 http(s)）；逐个重抓对齐 sha1
    candidates = [m.group(2) for m in _IMAGE_RE.finditer(md) if m.group(2).lower().startswith(("http://", "https://"))]
    digest_map: dict[str, str] = {}  # "raw/img-<sha10>.<ext>" → 原站 URL
    with httpx.Client(follow_redirects=True, timeout=20.0, headers=_HEADERS) as client:
        for url in candidates:
            if len(digest_map) >= len(unmapped):
                break
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception:
                continue
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                continue
            ext = _IMAGE_EXT.get(ctype, ".img")
            name = f"raw/img-{hashlib.sha1(resp.content).hexdigest()[:10]}{ext}"
            if name in unmapped and name not in digest_map:
                digest_map[name] = url

    if not digest_map:
        return 0, len(unmapped)

    new_files = [
        {"path": e, "src": digest_map[e]} if (isinstance(e, str) and e in digest_map) else e for e in raw_files
    ]
    if apply:
        guard = Guard(kb_root)
        meta["raw_files"] = new_files
        guard.write_json("normalize", meta_rel.as_posix(), meta)
    return len(digest_map), len(unmapped) - len(digest_map)


def main() -> int:
    ap = argparse.ArgumentParser(description="存量 meta.json raw_files 图片映射回填（默认 dry-run）")
    ap.add_argument("--kb-root", default="kb", help="kb 根目录（默认 kb）")
    ap.add_argument("--collection", default=None, help="限定 collection id（默认全部）")
    ap.add_argument("--apply", action="store_true", help="真实写盘（默认 dry-run 只报告）")
    args = ap.parse_args()

    kb_root = Path(args.kb_root)
    coll_root = kb_root / "collections"
    if not coll_root.exists():
        print(f"无 collections 目录：{coll_root}")
        return 1
    coll_ids = [args.collection] if args.collection else sorted(d.name for d in coll_root.iterdir() if d.is_dir())

    total_mapped = total_unresolved = total_pages = 0
    for cid in coll_ids:
        docs_root = coll_root / cid / "docs"
        if not docs_root.exists():
            continue
        metas = sorted(docs_root.rglob("meta.json"))
        print(f"[{cid}] {len(metas)} 个条目 meta")
        for meta_path in metas:
            rel = meta_path.relative_to(kb_root)  # 守卫写盘口径：相对 kb_root
            try:
                mapped, unresolved = migrate_entry(kb_root, rel, args.apply)
            except Exception as exc:
                print(f"  ! {rel}: {exc}")
                total_unresolved += 1
                continue
            total_pages += 1
            total_mapped += mapped
            total_unresolved += unresolved
            if mapped or unresolved:
                print(f"  {rel}: 回填 {mapped}，未解析 {unresolved}")

    mode = "已写盘" if args.apply else "dry-run（未写盘，加 --apply 执行）"
    print(f"\n完成：{total_pages} 个条目含图片，回填映射 {total_mapped} 条，未解析 {total_unresolved} 条（{mode}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
