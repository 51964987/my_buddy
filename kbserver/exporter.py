"""D 类 collection 全文导出（§6 v0.30，v0.32 标题命名定稿，用户拍板四项口径）。

只读派生物：从 toc.json / collection.json / docs/** 读取，不经写边界（铁律的写
边界只约束写盘）。三种形态：

- zip：中文标题布局——目录树目录标题=文件夹、页面标题=`<标题>.md`；正文去
  frontmatter 为纯原文，`# 标题` 下附灰色原站 URL 行；图片集中到所在目录 raw/
  （sha1 内容命名保证跨页合并零冲突，正文相对引用零改写、离线可用）；同层重名
  追加 -2/-3；error 页占位 md；toc.md 作索引（链接同步新布局）。
- merged：单文件 Markdown——目录树作大纲标题层级，页面正文内联并按大纲深度降级
  （围栏代码块感知），每页附原站链接；图片三模式 images=original（默认，按
  meta.json 映射改写回原站 URL，无映射回落 image API URL）/ relative / api；
  error 页（collection.json sync.errors，不在 toc.json）按标题占位"该页抓取失败"。
- pages：zip 的 docx 变体数据源——全量页面 JSON（zip 内路径 zip_path 由后端统一
  计算，命名单一事实源），前端逐页转 docx 后打包；docx 生成在前端复用唯一转换器
  （docx-render.ts），Python 侧不重复实现 md→docx。

大纲排序与前端 buildTree 同口径：兄弟节点按 toc.json index（v0.25 源站排序键）
升序，error 页无 index 排在本层末尾。
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from typing import Any

from .frontmatter import split_note

_HEADING_MAX = 6


# ---------------- 大纲树构建（与前端 buildTree 同口径） ----------------


def _new_node(kind: str, path: str, title: str, index: int) -> dict:
    return {"kind": kind, "path": path, "title": title, "index": index, "children": []}


def build_outline(toc: dict, errors: list[dict]) -> list[dict]:
    """toc.json + sync.errors → 嵌套大纲树（先目录后页面混合排序，error 页殿后）。"""
    dir_info = {d["path"]: d for d in toc.get("dirs", [])}
    root = _new_node("root", "", "", 0)

    def ensure_dir(path: str) -> dict:
        if not path:
            return root
        segs = path.split("/")
        node = root
        for i, seg in enumerate(segs):
            rel = "/".join(segs[: i + 1])
            child = next((c for c in node["children"] if c["path"] == rel), None)
            if child is None:
                d = dir_info.get(rel)
                child = _new_node("dir", rel, d["title"] if d else seg, d["index"] if d else 0)
                node["children"].append(child)
            node = child
        return node

    for p in toc.get("pages", []):
        parent = ensure_dir("/".join(p["path"].split("/")[:-1]))
        parent["children"].append(
            {
                "kind": "page",
                "path": p["path"],
                "title": p.get("title") or p["path"],
                "url": p.get("url") or "",
                "index": p.get("index") or 0,
                "children": [],
            }
        )
    # error 页：不在 toc.json（抓取/归一化失败），占位标注（用户拍板 3）
    for e in errors or []:
        parent = ensure_dir("/".join(e["path"].split("/")[:-1]))
        parent["children"].append(
            {
                "kind": "error",
                "path": e["path"],
                "title": e.get("title") or e["path"],
                "url": "",
                "index": 0,
                "error_message": e.get("error_message") or "",
                "children": [],
            }
        )

    def sort_rec(node: dict) -> None:
        node["children"].sort(key=lambda c: (c["kind"] == "error", c["index"], c["path"]))
        for c in node["children"]:
            sort_rec(c)

    sort_rec(root)
    return root["children"]


def _iter_markdown_lines(body: str):
    """逐行产出 (line, is_plain)：is_plain=False 为围栏代码块行（含围栏本身），原样保留。

    _shift_headings 与 _rewrite_image_links 共用同一围栏感知口径（v0.31）。
    """
    fence = False
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            fence = not fence
            yield line, False
            continue
        yield line, not fence


def _shift_headings(body: str, n: int) -> str:
    """正文标题降级 n 级（上限 6 级封顶）；围栏代码块内的行原样保留。"""
    if n <= 0:
        return body
    out: list[str] = []
    for line, plain in _iter_markdown_lines(body):
        stripped = line.lstrip()
        if plain and stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if 0 < hashes and stripped[hashes : hashes + 1] in (" ", "\t", ""):
                level = min(_HEADING_MAX, hashes + n)
                out.append("#" * level + stripped[hashes:])
                continue
        out.append(line)
    return "\n".join(out)


def _collect_stats(outline: list[dict]) -> tuple[int, int]:
    pages = errors = 0
    for n in outline:
        if n["kind"] == "page":
            pages += 1
        elif n["kind"] == "error":
            errors += 1
        p, e = _collect_stats(n["children"])
        pages += p
        errors += e
    return pages, errors


def _walk(outline: list[dict], depth: int):
    for n in outline:
        yield n, depth
        yield from _walk(n["children"], depth + 1)


# ---------------- 中文标题命名（v0.32，用户拍板：目录标题=文件夹、页面标题=文件名） ----------------

# Windows 文件名非法字符 + 控制符（平台约束：目标环境 Windows）
_WIN_BAD_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_SEG_MAX = 100  # 单段长度上限（MAX_PATH 防线，同 §5.3 第 6 条口径）


def _clean_segment(title: str, fallback: str) -> str:
    """标题 → 单个 zip 内路径段：非法字符替换为空格、剥尾点/空格、保留名加前缀、限长。"""
    s = _WIN_BAD_RE.sub(" ", title).strip(" .")
    if not s:
        s = fallback
    if s.upper().split(".")[0] in _WIN_RESERVED:
        s = "_" + s
    return s[:_SEG_MAX].rstrip(" .") or fallback


def _unique_name(base: str, used: dict[str, int]) -> str:
    """同层 casefold 判重：第二个同名追加 -2、-3…（用户拍板，简单可预期）。"""
    key = base.casefold()
    n = used.get(key, 0)
    used[key] = n + 1
    return base if n == 0 else f"{base}-{n + 1}"


def _assign_names(outline: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """大纲树 → zip 内命名映射（确定性，同层页/目各自判重）。

    返回 (dir_paths, file_stems)：
    - dir_paths：目录原 path → zip 目录相对路径（"" 根目录不在映射）
    - file_stems：页面原 path → zip 文件 stem（不含扩展名）
    """
    dir_paths: dict[str, str] = {}
    file_stems: dict[str, str] = {}

    def walk(nodes: list[dict], zip_dir: str, used_dirs: dict, used_pages: dict) -> None:
        for n in nodes:
            base = _clean_segment(n["title"], n["path"].split("/")[-1])
            if n["kind"] == "dir":
                sub = "/".join((zip_dir, _unique_name(base, used_dirs))) if zip_dir else _unique_name(base, used_dirs)
                dir_paths[n["path"]] = sub
                walk(n["children"], sub, {}, {})
            else:
                stem = "/".join((zip_dir, _unique_name(base, used_pages))) if zip_dir else _unique_name(base, used_pages)
                file_stems[n["path"]] = stem

    # 根层页面预留 "toc"（toc.md 索引文件占用）
    walk(outline, "", {}, {"toc": 1})
    return dir_paths, file_stems


# ---------------- merged：单文件全文 Markdown ----------------

# 图片引用（raw/img-* 本地化产物口径，与 normalize._IMAGE_RE 同形状）；rel = raw/img-<sha1>.<ext>
_IMAGE_REF_RE = re.compile(r"(!\[[^\]]*\]\()(raw/img-[^)\s]+)((?:\s+\"[^\"]*\")?\))")


def _rewrite_image_links(body: str, resolve_fn: Any) -> str:
    """正文图片相对引用逐个经 resolve_fn(rel) 解析为新引用（围栏代码块内不改写）。

    resolve_fn 返回 None 表示保留原样（v0.32：relative 模式与无兜底的未映射图片）。
    """
    out: list[str] = []
    for line, plain in _iter_markdown_lines(body):
        if plain:
            line = _IMAGE_REF_RE.sub(
                lambda m: (m.group(1) + new + m.group(3)) if (new := resolve_fn(m.group(2))) is not None else m.group(0),
                line,
            )
        out.append(line)
    return "\n".join(out)


def build_merged_markdown(
    toc: dict,
    coll: dict,
    errors: list[dict],
    read_note: Any,  # Callable[[str], str | None]：入参页面 path，返回 note.md 全文或 None
    now: datetime | None = None,
    image_mode: str = "original",  # original（默认：映射→原站 URL，无映射回落 api）/ relative / api
    image_map: Any = None,  # Callable[[str, str], str | None]：入参 (页面 path, 图片文件名)，返回原站 URL 或 None
    image_url: Any = None,  # Callable[[str, str], str] | None：入参 (页面 path, 图片 rel)，返回 image API URL
) -> str:
    now = now or datetime.now()
    outline = build_outline(toc, errors)
    pages, err_count = _collect_stats(outline)
    name = coll.get("name") or coll.get("id") or "collection"

    def resolve(page_path: str):
        if image_mode == "api":
            return lambda rel: image_url(page_path, rel) if image_url else None
        if image_mode == "original":
            def fn(rel: str) -> str | None:
                # 映射命中 → 原站 URL（用户拍板"打开 md 可按原始地址访问图片"）；
                # 未映射（存量未迁移/迁移失败）→ 回落 image API URL，维持图片可达
                src = image_map(page_path, rel.split("/")[-1]) if image_map else None
                if src:
                    return src
                return image_url(page_path, rel) if image_url else None
            return fn
        return lambda rel: None  # relative：保留 raw/img-* 相对引用（配 zip 使用）

    lines: list[str] = [
        f"# {name}（全文导出）",
        "",
        f"> 导出于 {now.strftime('%Y-%m-%d')}，共 {pages} 页"
        + (f"（{err_count} 页抓取失败，已占位标注）。" if err_count else "。"),
        "> 图片为原站地址（个别未映射图片指向本机服务端点）；离线完整图片请使用 zip 导出版本。",
        "",
    ]
    for node, depth in _walk(outline, 1):
        level = min(_HEADING_MAX, depth + 1)
        hashes = "#" * level
        if node["kind"] == "dir":
            lines += [f"{hashes} {node['title']}", ""]
            continue
        if node["kind"] == "error":
            lines += [
                f"{hashes} {node['title']}",
                "",
                f"> ⚠ 该页抓取失败：{node['error_message']}",
                "",
            ]
            continue
        lines += [f"{hashes} {node['title']}", ""]
        if node["url"]:
            # Markdown 链接（非纯文本）：docx 转换器才能产出真正的可点击超链接
            lines += [f"[原文]({node['url']})", ""]
        text = read_note(node["path"])
        if text is None:
            # toc 在册但正文缺失（如归一化失败清理）：与 error 页同口径占位
            lines += ["> ⚠ 该页抓取失败：正文文件缺失。", ""]
            continue
        _, body = split_note(text)
        body = body.strip()
        body = _rewrite_image_links(body, resolve(node["path"]))
        lines += [_shift_headings(body, level), ""]
    return "\n".join(lines) + "\n"


# ---------------- zip：中文标题布局 + 纯原文 + 图片 ----------------


def _export_page_md(title: str, url: str, body: str) -> str:
    """zip 内页面 md：无 frontmatter 纯原文，标题下附灰色原站 URL 行（用户拍板）。"""
    lines = [f"# {title}", ""]
    if url:
        lines += [f'<span style="color:#888888">原文：{url}</span>', ""]
    lines += [body.strip(), ""]
    return "\n".join(lines) + "\n"


def build_toc_md(
    outline: list[dict],
    name: str,
    now: datetime | None = None,
    names: tuple[dict[str, str], dict[str, str]] | None = None,  # _assign_names 产物
    ext: str = "md",
) -> str:
    now = now or datetime.now()
    pages, err_count = _collect_stats(outline)
    _, file_stems = names if names else (None, {})  # 缺省时页面链接口径回退（仅测试用）
    lines = [
        f"# {name} 目录树",
        "",
        f"> 导出于 {now.strftime('%Y-%m-%d')}，共 {pages} 页"
        + (f"（{err_count} 页抓取失败，占位见对应标题）。" if err_count else "。"),
        "> 页面 md/docx 文件在各目录下（标题命名），图片在其所在目录 raw/ 子目录。",
        "",
    ]
    for node, depth in _walk(outline, 0):
        indent = "  " * depth
        if node["kind"] == "dir":
            lines.append(f"{indent}- **{node['title']}**")
        elif node["kind"] == "error":
            lines.append(f"{indent}- {node['title']}（该页抓取失败）")
        else:
            stem = file_stems.get(node["path"], f"docs/{node['path']}/note")
            lines.append(f"{indent}- [{node['title']}]({stem}.{ext})（[原文]({node['url']})）")
    return "\n".join(lines) + "\n"


def build_zip(
    toc: dict,
    coll: dict,
    errors: list[dict],
    read_note: Any,  # Callable[[str], str | None]
    list_images: Any,  # Callable[[str], list[str]]：入参页面 path，返回图片文件名列表
    image_bytes: Any,  # Callable[[str, str], bytes]：入参 (path, 文件名)
    now: datetime | None = None,
) -> bytes:
    """zip 内容（v0.32 中文标题布局）：

        toc.md
        <目录标题>/…/<页面标题>.md      # 无 frontmatter 纯原文 + 灰色原文 URL 行
        <目录标题>/…/raw/img-*          # 该目录下全部页面的图片（sha1 内容命名，跨页同名即同内容）
        raw/img-*                       # 根层页面的图片

    只收录 raw/img-* 图片（_localize_images 命名口径）；page.json 等 raw 原件与
    archive/ 归档不入导出包（体积考虑，正文完整性不受影响）。
    """
    now = now or datetime.now()
    outline = build_outline(toc, errors)
    dir_paths, file_stems = _assign_names(outline)
    name = coll.get("name") or coll.get("id") or "collection"
    buf = io.BytesIO()
    written_images: set[str] = set()  # <zip 目录>/<img> 去重（sha1 内容命名：同名即同内容）
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("toc.md", build_toc_md(outline, name, now, names=(dir_paths, file_stems)))
        for node, _depth in _walk(outline, 0):
            if node["kind"] == "dir":
                continue
            stem = file_stems[node["path"]]
            zip_dir = stem.rsplit("/", 1)[0] if "/" in stem else ""
            if node["kind"] == "error":
                # 占位 md：保持目录树完整性（用户拍板 error 页不静默丢失）
                zf.writestr(
                    f"{stem}.md",
                    _export_page_md(node["title"], "", f"该页抓取失败：{node['error_message']}"),
                )
                continue
            text = read_note(node["path"])
            if text is None:
                # toc 在册但正文缺失：与 error 页同口径占位
                zf.writestr(f"{stem}.md", _export_page_md(node["title"], node["url"], "该页抓取失败：正文文件缺失。"))
                continue
            _, body = split_note(text)
            zf.writestr(f"{stem}.md", _export_page_md(node["title"], node["url"], body))
            for img in list_images(node["path"]):
                key = f"{zip_dir}/{img}"
                if key not in written_images:
                    written_images.add(key)
                    zf.writestr(f"{zip_dir}/raw/{img}", image_bytes(node["path"], img))
    return buf.getvalue()


# ---------------- pages：zip 的 docx 变体数据源（前端逐页转 docx） ----------------


def build_pages_payload(
    toc: dict,
    coll: dict,
    errors: list[dict],
    read_note: Any,  # Callable[[str], str | None]
    ext: str = "docx",
    now: datetime | None = None,
) -> dict:
    """全量页面 JSON：zip_path 由后端统一计算（与 build_zip 同一命名事实源），

    前端逐页渲染 docx 后按 zip_path 打包；body=None 表示正文缺失（前端占位）。
    """
    outline = build_outline(toc, errors)
    dir_paths, file_stems = _assign_names(outline)
    name = coll.get("name") or coll.get("id") or "collection"
    pages: list[dict] = []
    for node, _depth in _walk(outline, 0):
        if node["kind"] == "dir":
            continue
        item = {
            "kind": node["kind"],
            "path": node["path"],
            "title": node["title"],
            "zip_path": file_stems[node["path"]],
            "url": node.get("url") or "",
        }
        if node["kind"] == "error":
            item["error_message"] = node.get("error_message") or ""
        else:
            text = read_note(node["path"])
            item["body"] = split_note(text)[1].strip() if text else None
        pages.append(item)
    return {
        "name": name,
        "pages": pages,
        "toc_md": build_toc_md(outline, name, now, names=(dir_paths, file_stems), ext=ext),
    }
