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
    raw_files: list[str],
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
        raw_files.append(f"raw/{name}")
        return m.group(1) + f"raw/{name}" + m.group(3)

    return _IMAGE_RE.sub(repl, markdown)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_entry(inbox_dir: Path, guard: Guard, cfg: dict, fetcher: FetchFn) -> dict:
    payload = json.loads((inbox_dir / "payload.json").read_text(encoding="utf-8"))
    capture = json.loads((inbox_dir / "capture.json").read_text(encoding="utf-8"))
    captured_at = capture["captured_at"]
    url = (payload.get("url") or "").strip()

    ncfg = cfg.get("normalize", {})

    fr: FetchResult | None = None
    raw_files: list[str] = []
    html_bytes: bytes | None = None

    if url:
        try:
            fr = fetcher(url)
        except Exception as exc:
            raise NormalizeError("fetch", f"{type(exc).__name__}: {exc}") from exc
        canonical = canonicalize_url(fr.final_url)
        entry_id = url_to_id(canonical)
        platform, source_type = detect_platform(canonical)
        html = fr.text
        markdown = extract_markdown(html)
        title = (payload.get("title") or "").strip() or extract_title(html)
        html_bytes = fr.content
    else:
        text = (payload.get("text") or payload.get("selected_text") or "").strip()
        if not text:
            raise NormalizeError("extract", "payload has neither url nor text")
        canonical = ""
        entry_id = inbox_dir.name
        platform, source_type = "web", "social"
        markdown = text
        title = (payload.get("title") or "").strip() or text.splitlines()[0][:80]

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

    if url:
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
                "fetched_at": _now_iso(),
            }
            if fr
            else None
        ),
        "captured_from": capture.get("entry"),
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
