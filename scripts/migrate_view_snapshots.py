"""存量条目可浏览快照回填（§5.1 v0.43，默认 dry-run）。

用法（用启动服务的那个解释器跑）：
    python -X utf8 scripts/migrate_view_snapshots.py           # 只盘点，不写盘
    python -X utf8 scripts/migrate_view_snapshots.py --apply   # 经守卫写盘

口径：仅处理 sources/ 下有 raw/page.html、且其字节为 SPA 空壳
（剥脚本后可见文本 < 200 字符）、尚无 page.view.html 的条目；
渲染 URL 取 meta.original_url（缺失回落 frontmatter url），
经 Playwright 渲染后按 _build_view_snapshot 构造，失败降级对原始件剥脚本。
D 类 collection 页面 raw 为 page.json，不在此列。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kbserver.guard import Guard  # noqa: E402
from kbserver.normalize import (  # noqa: E402
    _build_view_snapshot,
    _looks_like_shell,
    playwright_fetch,
)

KB_ROOT = Path(__file__).resolve().parent.parent / "kb"


def note_url(entry_dir: Path) -> str:
    """渲染 URL：meta.original_url 优先，缺失回落 frontmatter url。"""
    meta = json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))
    url = (meta.get("original_url") or "").strip()
    if url:
        return url
    text = (entry_dir / "note.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("url: "):
            return line[5:].strip().strip("'\"")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写盘（默认 dry-run）")
    args = ap.parse_args()

    guard = Guard(KB_ROOT)
    targets: list[Path] = []
    skipped = 0
    for meta_path in sorted((KB_ROOT / "sources").glob("*/*/*/meta.json")):
        entry_dir = meta_path.parent
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw_files = meta.get("raw_files") or []
        if "raw/page.view.html" in raw_files:
            continue
        page_html = entry_dir / "raw" / "page.html"
        if not page_html.exists():
            skipped += 1
            continue
        if not _looks_like_shell(page_html.read_text(encoding="utf-8", errors="replace")):
            skipped += 1
            continue
        targets.append(entry_dir)

    print(f"扫描完成：待回填 {len(targets)} 条，跳过 {skipped} 条（非空壳/已有快照/无 page.html）")
    ok = 0
    for entry_dir in targets:
        rel = entry_dir.relative_to(KB_ROOT).as_posix()
        url = note_url(entry_dir)
        meta = json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))
        original = (entry_dir / "raw" / "page.html").read_text(encoding="utf-8", errors="replace")
        source = original
        via = "original"
        if url:
            try:
                source = playwright_fetch(url, timeout=20.0).text
                via = "playwright"
            except Exception as exc:
                print(f"  [warn] {rel}: 渲染失败降级原始件剥脚本：{type(exc).__name__}: {exc}")
        else:
            print(f"  [warn] {rel}: 无可渲染 URL，降级原始件剥脚本")
        view = _build_view_snapshot(source, url)
        print(f"  {'[apply]' if args.apply else '[dry]'} {rel}: via={via} url={url}")
        if args.apply:
            guard.write_bytes("normalize", f"{rel}/raw/page.view.html", view.encode("utf-8"))
            raw_files: list = meta.get("raw_files") or []
            if "raw/page.view.html" not in raw_files:
                raw_files.append("raw/page.view.html")
                guard.write_json("normalize", f"{rel}/meta.json", {**meta, "raw_files": raw_files})
            ok += 1
    if args.apply:
        print(f"回填完成：成功 {ok} 条")
    else:
        print("dry-run 未写盘；确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
