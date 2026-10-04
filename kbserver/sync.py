"""D 类同步引擎（§11.4 驻外联络员 / §5.2 sync 实施口径 v0.16）。

注册 collection 即全量首抓；增量同步经 Collection 任务 API 手动下发，
本模块无后台线程（线程挂载在编排器）。流程：

    目录树抓取 → toc.json 快照（每页 content_hash）→ diff
    → added/changed 记 changed_pages 交归一化落盘（entry: crawler）
    → removed 仅从 toc.json 移除并记 sync.removed_pages（落盘文件保留人工裁决）

站点解析器按站点注册（PARSERS 注册表），首个实例火山引擎文档站：
经其公开 JSON API（getDocList 目录树 / getDocDetail 正文 MDContent 直出）
获取结构化数据，纯 SPA 不抓 HTML 壳，无需 Playwright。

D 类页面 id 依据 §9 决策 1：SHA-1(collection-id + CanonicalURL 站内路径) 前 12 位。
章节落盘路径 = 目录树 code 链路清洗后的 ASCII 相对路径（限长，必要时编号化，见 5.3 第 6 条）。
所有写盘经写边界守卫（sync 阶段 → collections/<id>/；页面落盘走 normalize 阶段）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from .guard import Guard
from .idgen import collection_page_id
from .normalize import extract_markdown, normalize_collection_page

# 章节落盘路径约束（§5.3 第 6 条：Windows MAX_PATH 提前设防）
MAX_DOC_PATH_LEN = 160
MAX_SEG_LEN = 60

# 站点 API 请求间隔（秒）：礼貌抓取，不给对方站点压力
PAGE_FETCH_INTERVAL = 0.2


class SyncError(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


# ---------------- 站点解析器协议与数据结构 ----------------


@dataclass
class SiteNode:
    """目录树节点：目录或页面（is_dir 区分）；index 为源站排序键（升序，兄弟排序依据）。"""

    doc_id: int
    code: str
    parent_id: int
    is_dir: bool
    title: str
    index: int = 0


@dataclass
class SitePage:
    """单页抓取结果：markdown 为站点直出或转换后的正文。"""

    url: str  # canonical 全 URL（站内路径用于生成 id）
    title: str
    markdown: str
    raw: bytes | None = None  # API 原始响应体（存 raw/page.json）


class SiteParser(Protocol):
    def fetch_toc(self, collection: dict) -> list[SiteNode]: ...

    def fetch_page(self, collection: dict, node: SiteNode) -> SitePage: ...


# ---------------- 火山引擎文档站解析器（首个实例） ----------------

VOLC_BASE = "https://docs.volcengine.com"


def _volc_api_get(path: str, params: dict) -> dict:
    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; kbserver/0.1; personal kb sync)",
        "Accept": "application/json",
    }
    with httpx.Client(follow_redirects=True, timeout=30.0, headers=headers) as client:
        resp = client.get(f"{VOLC_BASE}{path}", params=params)
        resp.raise_for_status()
        data = resp.json()
    err = (data.get("ResponseMetadata") or {}).get("Error")
    if err:
        raise SyncError("fetch", f"volcengine api error: {err.get('Code')}: {err.get('Message')}")
    return data


class VolcengineDocsParser:
    """火山引擎文档站（docs.volcengine.com）。

    目录树：GET /api/doc/getDocList?LibraryCode=<code>&lang=<lang>
      返回扁平节点表，Type 区分目录(1)/页面(0)，节点含 DocumentID/ParentID/Title。
    正文：GET /api/doc/getDocDetail?LibraryCode=<code>&DocumentID=<id>&lang=<lang>
      ContentType=md 时 MDContent 直出 Markdown；其余类型回落 Content(HTML)
      走归一化提取器。CanonicalURL 为站内规范路径。
    """

    name = "volcengine"

    # 站点 URL → library_code（连接向导用，§11.5 v0.22）
    _URL_RE = re.compile(r"docs\.volcengine\.com/docs/([\w.-]+)", re.IGNORECASE)

    def probe_url(self, url: str) -> dict | None:
        """从站点 URL 提取站点参数；不匹配返回 None（连接向导据此判定适配器归属）。"""
        m = self._URL_RE.search(url or "")
        return {"library_code": m.group(1)} if m else None

    def _params(self, collection: dict) -> dict:
        site = collection.get("site") or {}
        code = site.get("library_code")
        if not code:
            raise SyncError("config", "collection.site.library_code is required")
        return {"LibraryCode": code, "lang": site.get("lang", "zh")}

    def fetch_toc(self, collection: dict) -> list[SiteNode]:
        data = _volc_api_get("/api/doc/getDocList", self._params(collection))
        nodes: list[SiteNode] = []
        for item in data.get("Result") or []:
            doc_id = item.get("DocumentID")
            code = (item.get("DocumentCode") or "").strip()
            if doc_id is None or not code:
                continue
            nodes.append(
                SiteNode(
                    doc_id=int(doc_id),
                    code=code,
                    parent_id=int(item.get("ParentID") or 0),
                    is_dir=item.get("Type") == 1,
                    title=(item.get("Title") or "").strip(),
                    index=int(item.get("Index") or 0),
                )
            )
        if not nodes:
            raise SyncError("fetch", "volcengine getDocList returned no nodes")
        return nodes

    def fetch_page(self, collection: dict, node: SiteNode) -> SitePage:
        params = self._params(collection)
        params["DocumentID"] = node.doc_id
        data = _volc_api_get("/api/doc/getDocDetail", params)
        result = data.get("Result") or {}
        title = (result.get("Title") or node.title).strip()
        content_type = (result.get("ContentType") or "").lower()
        if content_type == "md":
            markdown = (result.get("MDContent") or "").strip()
        else:
            # 非 md 内容按 HTML 走归一化提取器（§5.3 第 2 条：公开文档站结构规整）
            markdown = extract_markdown(result.get("Content") or "")
        if not markdown:
            raise SyncError("extract", f"page {node.doc_id} has no content")
        # v0.27 实测修正：站点 API 的 CanonicalURL 与实际路由不符（含 /doc/ 段，浏览器实测 404）；
        # 站点真实 URL = /docs/{LibraryCode}/{DocumentCode}（DocumentCode 全表唯一，单段可路由）。
        url = f"{VOLC_BASE}/docs/{params['LibraryCode']}/{node.code}?lang={params.get('lang', 'zh')}"
        raw = data if isinstance(data, dict) else {}
        return SitePage(
            url=url,
            title=title,
            markdown=markdown,
            raw=json.dumps(raw, ensure_ascii=False).encode("utf-8"),
        )


PARSERS: dict[str, SiteParser] = {
    VolcengineDocsParser.name: VolcengineDocsParser(),
}


def list_parsers() -> list[dict]:
    """解析器注册表只读清单（§11.5 v0.17：注册在代码内，新增站点 = 新增 adapter）。

    v0.18 增必填字段元数据；v0.22 升级为 fields（name/hint/placeholder，示例提示随
    adapter 声明），注册表单据此动态渲染，前端零硬编码（UI 纪律 3）。
    """
    field_meta = {
        "library_code": {
            "hint": "站点 URL 中 /docs/<这段>/ 的编号，如 docs.volcengine.com/docs/bytehouse-x/doc/… 取 bytehouse-x；可访问 getDocList?LibraryCode=<code>&lang=zh 自验",
            "placeholder": "如 bytehouse-x",
        },
    }
    required = {"volcengine": ["library_code"]}
    out = []
    for name, parser in PARSERS.items():
        doc = (type(parser).__doc__ or "").strip().splitlines()
        fields = [
            {"name": f, "hint": field_meta.get(f, {}).get("hint", "该解析器要求的站点参数"), "placeholder": field_meta.get(f, {}).get("placeholder", "")}
            for f in required.get(name, [])
        ]
        out.append(
            {
                "name": name,
                "description": doc[0].strip() if doc else "",
                "fields": fields,
            }
        )
    return out


def probe_site(url: str, parsers: dict | None = None, verify: bool = True) -> dict:
    """站点适配器探测（§11.5 v0.22 连接向导）：URL 逐个匹配注册表 adapter 的
    probe_url，命中即返回解析器名与站点参数；verify=True 时实测一次目录树请求
    验证参数有效性（返回节点统计或验证错误）。未命中 → matched=False（需先开发 adapter）。
    """
    parsers = parsers if parsers is not None else PARSERS
    for name, parser in parsers.items():
        probe = getattr(parser, "probe_url", None)
        params = probe(url) if probe else None
        if params is None:
            continue
        result: dict = {"matched": True, "parser": name, "params": params}
        if verify:
            try:
                nodes = parser.fetch_toc({"site": {**params, "lang": "zh"}})
                result["toc_nodes"] = len(nodes)
                result["toc_pages"] = sum(1 for n in nodes if not n.is_dir)
            except SyncError as exc:
                result["verify_error"] = exc.message
        return result
    return {"matched": False, "parser": None}


# ---------------- 路径清洗与目录树 → 落盘路径 ----------------


def _sanitize_segment(code: str) -> str:
    seg = re.sub(r"[^A-Za-z0-9_-]+", "-", code.strip())
    seg = seg.strip("-")
    if not seg:
        seg = "n"
    return seg[:MAX_SEG_LEN]


def _build_paths(nodes: list[SiteNode]) -> tuple[dict[int, str], dict[int, str]]:
    """目录树 code 链路 → ASCII 相对路径（5.3 第 6 条清洗），页面与目录一并解析。

    返回 (页面 doc_id→rel, 目录 doc_id→rel)；页面做冲突编号化与超长哈希截断，
    目录路径理论冲突由消费方去重。build_page_paths 为兼容包装。
    """
    by_id = {n.doc_id: n for n in nodes}
    rel_cache: dict[int, str] = {}

    def rel_of(node: SiteNode) -> str:
        if node.doc_id in rel_cache:
            return rel_cache[node.doc_id]
        if node.parent_id == 0 or node.parent_id not in by_id:
            rel = _sanitize_segment(node.code)
        else:
            parent = by_id[node.parent_id]
            base = rel_of(parent) if parent.is_dir else ""
            rel = f"{base}/{_sanitize_segment(node.code)}" if base else _sanitize_segment(node.code)
        rel_cache[node.doc_id] = rel
        return rel

    raw_paths: dict[int, str] = {}
    dir_paths: dict[int, str] = {}
    for node in nodes:
        if node.is_dir:
            dir_paths[node.doc_id] = rel_of(node)
            continue
        parent_part = rel_of(by_id[node.parent_id]) if node.parent_id in by_id else ""
        full = f"{parent_part}/{_sanitize_segment(node.code)}" if parent_part else _sanitize_segment(node.code)
        raw_paths[node.doc_id] = full

    # 同路径冲突 → 追加 document_id 编号化（确定性）
    seen: dict[str, int] = {}
    result: dict[int, str] = {}
    for doc_id in sorted(raw_paths):
        path = raw_paths[doc_id]
        if path in seen:
            path = f"{path}-{doc_id}"
        seen[path] = doc_id
        # 超 MAX_PATH 防线：保留头部段 + 内容哈希后缀（确定性、可复现）
        if len(path) > MAX_DOC_PATH_LEN:
            path = f"{path[:MAX_DOC_PATH_LEN]}~{hashlib.sha1(raw_paths[doc_id].encode()).hexdigest()[:8]}"
        result[doc_id] = path
    return result, dir_paths


def build_page_paths(nodes: list[SiteNode]) -> dict[int, str]:
    """目录树 code 链路 → ASCII 相对路径；冲突编号化、超长哈希截断（5.3 第 6 条）。"""
    return _build_paths(nodes)[0]


# ---------------- 同步引擎 ----------------


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _content_hash(markdown: str) -> str:
    return hashlib.sha1(markdown.encode("utf-8")).hexdigest()


class SyncEngine:
    def __init__(self, cfg, guard: Guard, parsers: dict[str, SiteParser] | None = None):
        self.cfg = cfg
        self.guard = guard
        self.parsers = parsers if parsers is not None else PARSERS

    # ---- collection.json 读写（均经守卫，sync 阶段） ----

    def _coll_rel(self, collection_id: str) -> str:
        return f"collections/{collection_id}/collection.json"

    def _toc_rel(self, collection_id: str) -> str:
        return f"collections/{collection_id}/toc.json"

    def _read_json(self, rel: str) -> dict | None:
        path = self.guard.kb_root.joinpath(*rel.split("/"))
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_json(self, rel: str, obj: dict) -> None:
        self.guard.write_json("sync", rel, obj)

    def register_collection(self, payload: dict) -> dict:
        """注册 collection（§4.3 schema）：校验 → 落 collection.json（不抓取）。"""
        collection_id = (payload.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", collection_id):
            raise SyncError("config", "collection id must match [a-z0-9][a-z0-9-]{1,63}")
        entry_url = (payload.get("entry_url") or "").strip()
        if not entry_url.lower().startswith(("http://", "https://")):
            raise SyncError("config", "entry_url must start with http:// or https://")
        toc_parser = (payload.get("toc_parser") or "").strip()
        if toc_parser not in self.parsers:
            raise SyncError("config", f"unknown toc_parser: {toc_parser}")

        from .platforms import detect_platform

        platform, _ = detect_platform(entry_url)
        library_code = (payload.get("library_code") or "").strip()
        if toc_parser == VolcengineDocsParser.name and not library_code:
            raise SyncError("config", "library_code is required for volcengine parser")

        collection = {
            "id": collection_id,
            "name": (payload.get("name") or "").strip() or collection_id,
            "entry_url": entry_url,
            # url_pattern：页面 URL 匹配规则，取站点域名 + 文档库路径前缀（§4.3）
            "url_pattern": f"{urlsplit(entry_url).hostname}/docs/{library_code}/" if library_code else urlsplit(entry_url).hostname,
            "toc_parser": toc_parser,
            "platform": platform,
            "site": {"library_code": library_code or None, "lang": payload.get("lang") or "zh"},
            "sync": {
                "state": "registered",
                "last_synced_at": None,
                "changed_pages": [],
                "removed_pages": [],
                "errors": [],
                "last_result": None,
                "last_error": None,
            },
        }
        self._write_json(self._coll_rel(collection_id), collection)
        return collection

    def get_collection(self, collection_id: str) -> dict | None:
        return self._read_json(self._coll_rel(collection_id))

    def get_toc(self, collection_id: str) -> dict | None:
        """目录树快照（文档树页数据源）；collection.json 缺失时同样返回 None（404 裁决在 API 层）。"""
        if self.get_collection(collection_id) is None:
            return None
        return self._read_json(self._toc_rel(collection_id))

    def list_collections(self) -> list[dict]:
        root = self.guard.kb_root / "collections"
        if not root.exists():
            return []
        out = []
        for child in sorted(root.iterdir()):
            coll = self.get_collection(child.name) if child.is_dir() else None
            if coll:
                out.append(coll)
        return out

    # ---- 同步主流程 ----

    def sync(self, collection_id: str) -> dict:
        """全量首抓（无 toc.json）或增量同步。返回同步结果摘要。"""
        coll = self.get_collection(collection_id)
        if coll is None:
            raise SyncError("config", f"collection not found: {collection_id}")
        parser = self.parsers.get(coll.get("toc_parser") or "")
        if parser is None:
            raise SyncError("config", f"unknown toc_parser: {coll.get('toc_parser')}")

        coll["sync"]["state"] = "syncing"
        self._write_json(self._coll_rel(collection_id), coll)
        try:
            summary = self._run(coll, parser)
        except Exception as exc:
            coll["sync"]["state"] = "error"
            coll["sync"]["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
            self._write_json(self._coll_rel(collection_id), coll)
            raise
        coll["sync"].update(
            state="ok",
            last_synced_at=_now_iso(),
            last_error=None,
            **summary["state_fields"],
        )
        coll["sync"]["last_result"] = summary["last_result"]
        self._write_json(self._coll_rel(collection_id), coll)
        return summary["last_result"]

    def _run(self, coll: dict, parser: SiteParser) -> dict:
        collection_id = coll["id"]
        old_toc = self._read_json(self._toc_rel(collection_id)) or {}
        old_pages = {p["path"]: p for p in old_toc.get("pages", [])}

        nodes = parser.fetch_toc(coll)
        paths, dir_rels = _build_paths(nodes)
        by_id = {n.doc_id: n for n in nodes}
        # 目录节点标题（v0.24：文档树页目录显示源站中文名；路径段为清洗后英文 code）
        # v0.25：dirs/pages 均带 index（源站排序键），前端兄弟节点按其升序排序
        dirs: list[dict] = []
        seen_dir_paths: set[str] = set()
        for node in nodes:
            if node.is_dir:
                rel = dir_rels[node.doc_id]
                if rel not in seen_dir_paths:
                    seen_dir_paths.add(rel)
                    dirs.append({"path": rel, "title": node.title, "index": node.index})
        page_nodes = [n for n in nodes if not n.is_dir]

        # 逐页抓取（content_hash diff 依据）+ 失败页记 errors（可观测、下次同步重试）
        fetched: dict[str, SitePage] = {}
        new_pages: list[dict] = []
        errors: list[dict] = []
        for i, node in enumerate(page_nodes):
            if i:
                time.sleep(PAGE_FETCH_INTERVAL)
            path = paths[node.doc_id]
            try:
                page = parser.fetch_page(coll, node)
            except Exception as exc:
                # v0.29：error 记录补 title/index（全文导出占位标注需要标题与大纲排序）
                errors.append(
                    {
                        "document_id": node.doc_id,
                        "path": path,
                        "title": node.title,
                        "index": node.index,
                        "error_message": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                continue
            new_pages.append(
                {
                    "path": path,
                    "title": page.title,
                    "url": page.url,
                    "document_id": node.doc_id,
                    "content_hash": _content_hash(page.markdown),
                    "index": node.index,
                }
            )
            fetched[path] = page

        new_map = {p["path"]: p for p in new_pages}
        added = sorted(set(new_map) - set(old_pages))
        changed = sorted(
            p for p in set(new_map) & set(old_pages)
            if new_map[p]["content_hash"] != old_pages[p].get("content_hash")
        )
        removed = sorted(set(old_pages) - set(new_map))

        # added/changed 逐页交归一化引擎落盘（§5.2；enrich 由编排器统一推进）
        outcomes = {"created": 0, "updated": 0, "duplicate": 0, "error": 0}
        captured_at = _now_iso()
        cfg = self.cfg.data if hasattr(self.cfg, "data") else dict(self.cfg)
        fetcher = self._make_fetcher()
        for path in added + changed:
            page = fetched[path]
            entry_rel = f"collections/{collection_id}/docs/{path}"
            try:
                result = normalize_collection_page(
                    self.guard,
                    cfg,
                    fetcher,
                    collection_id=collection_id,
                    entry_rel=entry_rel,
                    entry_id=collection_page_id(collection_id, path),
                    url=page.url,
                    title=page.title,
                    markdown=page.markdown,
                    raw_payload=page.raw,
                    platform=coll.get("platform"),
                    captured_at=captured_at,
                )
                outcomes[result["outcome"]] = outcomes.get(result["outcome"], 0) + 1
            except Exception as exc:
                outcomes["error"] += 1
                # v0.29：error 记录补 title/index（全文导出占位标注需要标题与大纲排序）；
                # 归一化失败发生在抓取成功之后，title 取已抓到的页面标题
                err_node = next((n for n in page_nodes if paths.get(n.doc_id) == path), None)
                errors.append(
                    {
                        "path": path,
                        "title": fetched[path].title if path in fetched else (err_node.title if err_node else path),
                        "index": err_node.index if err_node else 0,
                        "error_message": f"normalize {type(exc).__name__}: {exc}"[:300],
                    }
                )

        # URL 漂移修复（v0.27）：源站 URL 口径修正后，已落盘页面的 frontmatter url 需同步。
        # 走 sync 阶段字段级补丁只改 url——不动 status/ai.*（内容未变，不触发重新整理与 AI 调用）。
        # 注意 patch_note_fields 收 note.md 文件路径（非条目目录）。
        for p in new_pages:
            old = old_pages.get(p["path"])
            if old and old.get("url") != p["url"]:
                note_rel = f"collections/{collection_id}/docs/{p['path']}/note.md"
                if self.guard.exists(note_rel):
                    self.guard.patch_note_fields("sync", note_rel, {"url": p["url"]}, {"url"})

        # toc.json 快照原子写（含每页 content_hash 与目录节点标题，diff 与文档树展示依据）
        toc = {
            "generated_at": _now_iso(),
            "pages": new_pages,
            "dirs": dirs,
        }
        self._write_json(self._toc_rel(collection_id), toc)

        return {
            "state_fields": {
                "changed_pages": added + changed,
                "removed_pages": removed,
                "errors": errors,
            },
            "last_result": {
                "finished_at": _now_iso(),
                "pages": len(new_pages),
                "added": len(added),
                "changed": len(changed),
                "removed": len(removed),
                "errors": len(errors),
                "outcomes": outcomes,
                "changed_paths": added + changed,
                "removed_paths": removed,
            },
        }

    def _make_fetcher(self):
        """图片本地化等二次抓取用 fetcher；延迟导入避免循环依赖。"""
        from .normalize import http_fetch

        timeout = float(self.cfg.data.get("normalize", {}).get("fetch_timeout", 20.0)) if hasattr(self.cfg, "data") else 20.0
        return lambda u: http_fetch(u, timeout=timeout)
