"""归一化引擎（§11.4 翻译官）：inbox 原始 payload → sources 条目。

正文提取 → 转 Markdown → 相对链接后处理 → 图片本地化 raw/ →
frontmatter + meta.json（含 content_hash）→ 经写边界守卫落盘。
不判断内容价值、不调用 AI。raw 原件永不覆盖：同 id 内容更新时旧 raw 归档
（§5.3 第 4 条）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import trafilatura
import yaml
from bs4 import BeautifulSoup
from markdownify import markdownify

from .guard import Guard
from .idgen import canonicalize_url, url_to_id
from .platforms import detect_platform

FetchFn = Callable[[str], "FetchResult"]


@dataclass
class FetchResult:
    url: str
    final_url: str
    content: bytes
    encoding: str = "utf-8"
    status_code: int = 200
    content_type: str = ""
    redirect_chain: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


class NormalizeError(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


def http_fetch(url: str, timeout: float = 20.0) -> FetchResult:
    import httpx

    headers = {"User-Agent": "Mozilla/5.0 (compatible; kbserver/0.1; personal kb capture)"}
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        chain = [str(r.headers.get("location") or r.request.url) for r in resp.history]
        return FetchResult(
            url=url,
            final_url=str(resp.url),
            content=resp.content,
            encoding=resp.encoding or "utf-8",
            status_code=resp.status_code,
            content_type=resp.headers.get("content-type", ""),
            redirect_chain=chain,
        )


def playwright_fetch(url: str, timeout: float = 15.0) -> FetchResult:
    """动态页兜底抓取（§5.1 v0.17）：无头 Chromium 渲染后返回最终 DOM。

    - wait_until=networkidle 等 SPA 网络静止，再留少量渲染余量；
    - 重依赖延迟导入：未安装时抛带安装提示的 RuntimeError（调用方转 error
      可观测、可重跑，不静默跳过）；
    - 渲染后 HTML 即为 raw 原件（raw 升级为渲染后页面）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright not installed: pip install playwright && playwright install chromium"
        ) from exc
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (compatible; kbserver/0.1; personal kb capture)"
                )
                page.goto(url, timeout=int(timeout * 1000), wait_until="networkidle")
                page.wait_for_timeout(800)  # 网络静止后的渲染余量
                html = page.content()
                final_url = page.url
            finally:
                browser.close()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"playwright fetch failed: {type(exc).__name__}: {exc}") from exc
    if not html.strip():
        raise RuntimeError("playwright rendered empty page")
    return FetchResult(
        url=url,
        final_url=final_url,
        content=html.encode("utf-8"),
        encoding="utf-8",
        status_code=200,
        content_type="text/html",
    )


def extract_markdown(html: str) -> str:
    md = None
    try:
        md = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_images=True,
            include_tables=True,
            include_comments=False,
        )
    except Exception:
        md = None
    if md and md.strip():
        return md.strip()
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("article") or soup.find("main") or soup.body or soup
    text = markdownify(str(node), heading_style="ATX").strip()
    if not text:
        raise NormalizeError("extract", "no extractable content")
    return text


def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string and soup.title.string.strip():
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return ""


_LINK_RE = re.compile(r"(\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")
_IMAGE_RE = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")

_IMAGE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


def _absolutize_links(markdown: str, base_url: str) -> str:
    if not base_url:
        return markdown

    def repl(m: re.Match) -> str:
        href = m.group(2)
        if href.startswith(("data:", "mailto:", "#", "http://", "https://")):
            return m.group(0)
        return m.group(1) + urljoin(base_url, href) + m.group(3)

    return _LINK_RE.sub(repl, markdown)


def _localize_images(
    markdown: str,
    entry_rel: str,
    guard: Guard,
    fetcher: FetchFn,
    max_images: int,
    raw_files: list,
) -> str:
    counter = {"n": 0}

    def repl(m: re.Match) -> str:
        src = m.group(2)
        if not src.lower().startswith(("http://", "https://")):
            return m.group(0)
        if counter["n"] >= max_images:
            return m.group(0)
        counter["n"] += 1
        try:
            fr = fetcher(src)
        except Exception:
            return m.group(0)
        if not fr.content_type.lower().startswith("image/"):
            return m.group(0)
        ext = _IMAGE_EXT.get(fr.content_type.lower().split(";")[0].strip(), ".img")
        digest = hashlib.sha1(fr.content).hexdigest()[:10]
        name = f"img-{digest}{ext}"
        guard.write_bytes("normalize", f"{entry_rel}/raw/{name}", fr.content)
        # v0.32：图片条目记录原站 URL 映射（{path, src}），导出侧据此把 raw/img-* 引用改写回原址；
        # 非图片原件（page.html/page.json/screenshot）维持 str 形状
        raw_files.append({"path": f"raw/{name}", "src": src})
        return m.group(1) + f"raw/{name}" + m.group(3)

    return _IMAGE_RE.sub(repl, markdown)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _archive_old_raw(guard: Guard, entry_rel: str) -> None:
    """同 id 内容更新时旧 raw 归档（§5.3 第 4 条）：raw 原件永不覆盖。"""
    archive_ts = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    old_raw = guard.kb_root / entry_rel / "raw"
    if old_raw.exists():
        for child in sorted(old_raw.iterdir()):
            if child.name == "archive":
                continue
            guard.move_tree(
                "normalize",
                f"{entry_rel}/raw/{child.name}",
                f"{entry_rel}/raw/archive/{archive_ts}/{child.name}",
            )


def normalize_entry(
    inbox_dir: Path,
    guard: Guard,
    cfg: dict,
    fetcher: FetchFn,
    playwright_fetcher: FetchFn | None = None,
) -> dict:
    payload = json.loads((inbox_dir / "payload.json").read_text(encoding="utf-8"))
    capture = json.loads((inbox_dir / "capture.json").read_text(encoding="utf-8"))
    captured_at = capture["captured_at"]
    url = (payload.get("url") or "").strip()

    ncfg = cfg.get("normalize", {})

    fr: FetchResult | None = None
    raw_files: list = []  # 元素：str（非图片原件）| dict（图片 {path, src}，v0.32）
    html_bytes: bytes | None = None
    selection = (payload.get("text") or payload.get("selected_text") or "").strip()
    extraction = "full"
    fetch_via = "http"

    if url:
        title = (payload.get("title") or "").strip()
        markdown = ""
        # 最近一次失败描述（带阶段前缀 fetch:/extract:，用于 error 定性与信息）
        fetch_error: str | None = None

        # 1) 直连抓取 + 提取（失败不立即定论，交给下方降级/兜底裁决）
        try:
            fr = fetcher(url)
        except Exception as exc:
            fr = None
            fetch_error = f"fetch: {type(exc).__name__}: {exc}"
        if fr is not None:
            try:
                markdown = extract_markdown(fr.text)
            except NormalizeError as exc:
                markdown = ""
                fetch_error = f"{exc.stage}: {exc.message}"

        # 2) 动态页兜底（§5.1 v0.17）：仅无选中文本时触发（带选中文本走既有降级，
        #    避免无谓渲染开销）；抓取失败或提取为空时经 Playwright 渲染重试
        if (fr is None or not markdown.strip()) and not selection and ncfg.get("playwright_fallback", True):
            pw = playwright_fetcher or playwright_fetch
            stage = "fetch" if fr is None else "extract"
            try:
                fr = pw(url, timeout=float(ncfg.get("playwright_timeout", 15.0)))
                fetch_via = "playwright"
                markdown = extract_markdown(fr.text)
                fetch_error = None
            except NormalizeError as exc:
                fetch_error = f"{exc.stage}: playwright: {exc.message}"
            except Exception as exc:
                fetch_error = f"{stage}: playwright: {type(exc).__name__}: {exc}"

        # 3) 裁决：渲染/提取成功 → 完整落盘；有选中文本 → 降级；否则按失败阶段转 error
        if fr is not None and markdown.strip():
            canonical = canonicalize_url(fr.final_url)
            html_bytes = fr.content
            title = title or extract_title(fr.text)
        elif selection:
            canonical = canonicalize_url(url)
            markdown = selection
            extraction = "selection_fallback"
            if fr is not None:
                html_bytes = fr.content  # SPA 空壳等原始件仍保留（§5.3 第 2 条）
        elif fr is not None:
            raise NormalizeError("extract", fetch_error or "no extractable content")
        else:
            raise NormalizeError("fetch", fetch_error or "fetch failed")
        entry_id = url_to_id(canonical)
        platform, source_type = detect_platform(canonical)
    else:
        if not selection:
            raise NormalizeError("extract", "payload has neither url nor text")
        canonical = ""
        entry_id = inbox_dir.name
        platform, source_type = "web", "social"
        markdown = selection
        title = (payload.get("title") or "").strip() or selection.splitlines()[0][:80]

    content_hash = hashlib.sha1(markdown.encode("utf-8")).hexdigest()
    entry_rel = f"sources/{platform}/{captured_at[:4]}/{entry_id}"
    meta_rel = f"{entry_rel}/meta.json"

    outcome = "created"
    existed = guard.exists(meta_rel)
    if existed:
        try:
            existing = json.loads(
                guard.kb_root.joinpath(*meta_rel.split("/")).read_text(encoding="utf-8")
            )
        except Exception:
            existing = {}
        if existing.get("content_hash") == content_hash:
            return {
                "entry_id": entry_id,
                "outcome": "duplicate",
                "entry_rel": entry_rel,
                "platform": platform,
                "source_type": source_type,
                "title": title or entry_id,
                "url": canonical,
            }
        outcome = "updated"
        _archive_old_raw(guard, entry_rel)
        raw_files = []

    if html_bytes is not None:
        guard.write_bytes("normalize", f"{entry_rel}/raw/page.html", html_bytes)
        raw_files.append("raw/page.html")

    screenshot = payload.get("screenshot_b64")
    if screenshot:
        try:
            img = base64.b64decode(screenshot)
            guard.write_bytes("normalize", f"{entry_rel}/raw/screenshot.png", img)
            raw_files.append("raw/screenshot.png")
        except Exception:
            pass

    if url and fr is not None:
        markdown = _absolutize_links(markdown, fr.final_url)
        if ncfg.get("image_localization", True):
            markdown = _localize_images(
                markdown, entry_rel, guard, fetcher, int(ncfg.get("max_images", 30)), raw_files
            )

    fm: dict = {
        "id": entry_id,
        "platform": platform,
        "source_type": source_type,
        "captured_at": captured_at,
        "title": title or entry_id,
        "status": "normalized",
        "tags": [],
    }
    if canonical:
        fm["url"] = canonical
    note_md = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + markdown + "\n"

    meta: dict = {
        "content_hash": content_hash,
        "original_url": url or None,
        "redirect_chain": fr.redirect_chain if fr else [],
        "raw_files": raw_files,
        "fetch": (
            {
                "status_code": fr.status_code,
                "content_type": fr.content_type,
                "via": fetch_via,
                "fetched_at": _now_iso(),
            }
            if fr
            else None
        ),
        "captured_from": capture.get("entry"),
        "extraction": extraction,
    }

    guard.write_text("normalize", f"{entry_rel}/note.md", note_md)
    guard.write_json("normalize", meta_rel, meta)

    return {
        "entry_id": entry_id,
        "outcome": outcome,
        "entry_rel": entry_rel,
        "platform": platform,
        "source_type": source_type,
        "title": fm["title"],
        "url": canonical,
    }


def normalize_collection_page(
    guard: Guard,
    cfg: dict,
    fetcher: FetchFn,
    *,
    collection_id: str,
    entry_rel: str,
    entry_id: str,
    url: str,
    title: str,
    markdown: str,
    raw_payload: bytes | None = None,
    platform: str | None = None,
    captured_at: str | None = None,
) -> dict:
    """D 类变更页归一化（§5.2 sync 实施口径）：变更页交归一化引擎落盘。

    与 normalize_entry 共用提取后处理与落盘逻辑；差异点：
    - entry_rel 由 sync 引擎按目录树路径给出（collections/<id>/docs/<章节路径>）；
    - entry_id 依据 §9 决策 1 由 sync 引擎按 collection-id + 站内路径生成；
    - frontmatter 增 collection 字段，source_type 固定 product_doc；
    - 正文由站点 API 直出 Markdown 时跳过 trafilatura，仅做相对链接后处理；
    - raw 原件为站点 API 响应体（raw/page.json），满足"raw 永不覆盖"纪律。
    """
    if not markdown.strip():
        raise NormalizeError("extract", "collection page has empty markdown")
    captured_at = captured_at or _now_iso()
    canonical = canonicalize_url(url)
    if platform is None:
        platform, _ = detect_platform(canonical)

    ncfg = cfg.get("normalize", {})
    content_hash = hashlib.sha1(markdown.encode("utf-8")).hexdigest()
    meta_rel = f"{entry_rel}/meta.json"

    outcome = "created"
    raw_files: list = []  # 元素：str（非图片原件）| dict（图片 {path, src}，v0.32）
    existed = guard.exists(meta_rel)
    if existed:
        try:
            existing = json.loads(
                guard.kb_root.joinpath(*meta_rel.split("/")).read_text(encoding="utf-8")
            )
        except Exception:
            existing = {}
        if existing.get("content_hash") == content_hash:
            return {
                "entry_id": entry_id,
                "outcome": "duplicate",
                "entry_rel": entry_rel,
                "url": canonical,
            }
        outcome = "updated"
        _archive_old_raw(guard, entry_rel)

    if raw_payload is not None:
        guard.write_bytes("normalize", f"{entry_rel}/raw/page.json", raw_payload)
        raw_files.append("raw/page.json")

    markdown = _absolutize_links(markdown, url)
    if ncfg.get("image_localization", True):
        markdown = _localize_images(
            markdown, entry_rel, guard, fetcher, int(ncfg.get("max_images", 30)), raw_files
        )

    fm: dict = {
        "id": entry_id,
        "platform": platform,
        "source_type": "product_doc",
        "captured_at": captured_at,
        "title": title or entry_id,
        "status": "normalized",
        "tags": [],
        "collection": collection_id,
    }
    if canonical:
        fm["url"] = canonical
    note_md = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + markdown + "\n"

    meta: dict = {
        "content_hash": content_hash,
        "original_url": None,
        "redirect_chain": [],
        "raw_files": raw_files,
        "fetch": {
            "status_code": 200,
            "content_type": "application/json",
            "fetched_at": _now_iso(),
        },
        "captured_from": "crawler",
        "extraction": "full",
    }

    guard.write_text("normalize", f"{entry_rel}/note.md", note_md)
    guard.write_json("normalize", meta_rel, meta)

    return {
        "entry_id": entry_id,
        "outcome": outcome,
        "entry_rel": entry_rel,
        "platform": platform,
        "source_type": "product_doc",
        "title": fm["title"],
        "url": canonical,
    }
