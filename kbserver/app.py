"""API 网关（§11.1）：Capture / Collection 任务 / Query·运维 / 参数设置 四组路由。

token 鉴权；Collection 任务 API（P2c 起，§6/§5.2 实施口径）：注册即全量首抓、增量手动下发；
Query·运维 API 含检索（P4 起全文，v0.17 增语义模式）、索引重建、wiki 人工处置（curation
通道）、enrich 流水与解析器清单；Web UI（P5）经 StaticFiles 托管 kbserver/static（/app）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from . import curation
from . import exporter
from .capture import CaptureError, accept_capture
from .config import Config
from .deps import check_dependencies
from .enrich import EnrichError, EntryNotFound, EntryStateError
from .frontmatter import split_note
from .guard import Guard, WriteBoundaryError
from .indexer import Indexer, SemanticSearchError
from .maintenance import reset_regions
from .orchestrator import Orchestrator
from .platforms import platform_options, source_type_options
from .sync import SyncError, list_parsers, probe_site

KB_REGIONS = ("inbox", "sources", "collections", "wiki")

# image 端点 media type（raw/img-* 落盘扩展名口径，见 normalize._IMAGE_EXT；.img 兜底二进制）
_IMAGE_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
}


class CaptureRequest(BaseModel):
    url: str | None = None
    title: str | None = None
    text: str | None = None
    selected_text: str | None = None
    screenshot_b64: str | None = None
    entry: str = "cli"


class ConfigUpdateRequest(BaseModel):
    config: dict[str, Any]


class CollectionRequest(BaseModel):
    id: str
    name: str | None = None
    entry_url: str
    toc_parser: str = "volcengine"
    library_code: str | None = None
    lang: str = "zh"


class ResetRequest(BaseModel):
    regions: list[str]
    confirm: str
    backup: bool = False


class CollectionPatchRequest(BaseModel):
    # v0.50 集合元数据更新：name/entry_url 至少一项（§6）
    name: str | None = None
    entry_url: str | None = None


def create_app(cfg: Config | None = None, orchestrator: Orchestrator | None = None) -> FastAPI:
    cfg = cfg or Config()
    guard = Guard(cfg.kb_root)
    orch = orchestrator or Orchestrator(cfg, guard)
    indexer = Indexer(cfg, guard)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 启动期依赖自检（v0.36）：延迟导入的依赖（jieba 等）缺装不再留到运行期变成
        # 无信息 500 —— 必需依赖缺失直接拒绝启动，可选依赖缺失仅告警
        check_dependencies()
        for region in KB_REGIONS:
            (cfg.kb_root / region).mkdir(parents=True, exist_ok=True)
        if cfg.data.get("pipeline", {}).get("worker_enabled", True):
            orch.start()
        yield
        orch.stop()

    app = FastAPI(title="kbserver", version=__version__, lifespan=lifespan)

    def auth(x_kb_token: str | None = Header(default=None)):
        token = cfg.data.get("token") or ""
        if token and x_kb_token != token:
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/", dependencies=[Depends(auth)])
    def root():
        return {"service": "kbserver", "version": __version__, "docs": "/docs"}

    @app.post("/api/capture", dependencies=[Depends(auth)])
    def capture(req: CaptureRequest):
        payload = req.model_dump()
        if not payload.get("text") and payload.get("selected_text"):
            payload["text"] = payload["selected_text"]
        try:
            return accept_capture(guard, payload)
        except CaptureError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ---------- Collection 任务 API（P2c，§6/§5.2 实施口径） ----------

    @app.post("/api/collections", dependencies=[Depends(auth)])
    def register_collection(req: CollectionRequest):
        # 注册即全量首抓：落 collection.json 后下发同步（worker 停用时同步执行，便于测试）
        try:
            coll = orch.sync_engine.register_collection(req.model_dump())
        except SyncError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        trigger = orch.sync_enqueue(coll["id"])
        return {"registered": True, "collection": coll, "sync": trigger}

    @app.get("/api/collections", dependencies=[Depends(auth)])
    def list_collections():
        collections = orch.sync_engine.list_collections()
        return {"collections": collections, "total": len(collections)}

    @app.patch("/api/collections/{collection_id}", dependencies=[Depends(auth)])
    def update_collection(collection_id: str, req: CollectionPatchRequest):
        # 集合元数据更新（v0.50）：name/entry_url，至少一项；不触发重抓
        if orch.sync_engine.get_collection(collection_id) is None:
            raise HTTPException(status_code=404, detail=f"collection not found: {collection_id}")
        try:
            coll = orch.sync_engine.update_collection(collection_id, req.model_dump())
        except SyncError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"updated": True, "collection": coll}

    @app.get("/api/collections/{collection_id}/sync/progress", dependencies=[Depends(auth)])
    def collection_sync_progress(collection_id: str):
        # 首抓/同步实时进度（v0.51）：轻量端点（不扫 kb），前端注册后轮询此端点展示 done/total
        if orch.sync_engine.get_collection(collection_id) is None:
            raise HTTPException(status_code=404, detail=f"collection not found: {collection_id}")
        return {
            "collection_id": collection_id,
            "progress": orch.sync_engine.get_progress(collection_id),
        }

    @app.post("/api/collections/{collection_id}/sync", dependencies=[Depends(auth)])
    def sync_collection(collection_id: str):
        if orch.sync_engine.get_collection(collection_id) is None:
            raise HTTPException(status_code=404, detail=f"collection not found: {collection_id}")
        return {"collection_id": collection_id, "sync": orch.sync_enqueue(collection_id)}

    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        result = orch.status()
        result["index"] = indexer.index_status()
        result["version"] = __version__
        return result

    def _iter_notes(include_collections: bool = False):
        # 条目遍历：默认只扫 sources/（时间流清单口径，v0.20）；按 id 查详情需
        # 覆盖 collections/（D 类页面详情，文档树页 openPage 依赖，v0.26 补）
        for region in ["sources"] + (["collections"] if include_collections else []):
            root = cfg.kb_root / region
            if not root.exists():
                continue
            yield from sorted(root.rglob("note.md"))

    @app.get("/api/entries", dependencies=[Depends(auth)])
    def list_entries(
        platform: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
    ):
        entries = []
        for note in _iter_notes():
            fm, _ = split_note(note.read_text(encoding="utf-8"))
            if platform and fm.get("platform") != platform:
                continue
            if source_type and fm.get("source_type") != source_type:
                continue
            if status and fm.get("status") != status:
                continue
            entries.append(
                {
                    "id": fm.get("id"),
                    "title": fm.get("title"),
                    "url": fm.get("url"),
                    "platform": fm.get("platform"),
                    "source_type": fm.get("source_type"),
                    "status": fm.get("status"),
                    "captured_at": fm.get("captured_at"),
                    "tags": fm.get("tags", []),
                    "path": note.relative_to(cfg.kb_root).as_posix(),
                }
            )
        return {"entries": entries, "total": len(entries)}

    @app.get("/api/entries/{entry_id}", dependencies=[Depends(auth)])
    def get_entry(entry_id: str):
        # 详情按 id 查询须覆盖 sources + collections 两区（D 类页面 id 前端可复算，
        # 文档树页点击页面即依赖本端点；此前只扫 sources 致 D 类详情 404，v0.26 修）
        for note in _iter_notes(include_collections=True):
            fm, body = split_note(note.read_text(encoding="utf-8"))
            if fm.get("id") != entry_id:
                continue
            entry_dir = note.parent
            meta = {}
            meta_path = entry_dir / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            raw_files = sorted(p.relative_to(entry_dir).as_posix() for p in (entry_dir / "raw").rglob("*") if p.is_file()) if (entry_dir / "raw").exists() else []
            return {"frontmatter": fm, "body": body, "meta": meta, "raw_files": raw_files}
        raise HTTPException(status_code=404, detail=f"entry not found: {entry_id}")

    @app.post("/api/entries/{entry_id}/rerun", dependencies=[Depends(auth)])
    def rerun_entry(entry_id: str):
        # inbox error（normalize 阶段）与 sources error（enrich 阶段）二选一复活
        if orch.rerun(entry_id) or orch.rerun_enrich(entry_id):
            return {"rerun": True, "entry_id": entry_id}
        raise HTTPException(status_code=404, detail=f"no rerunnable error entry: {entry_id}")

    @app.delete("/api/entries/{entry_id}", dependencies=[Depends(auth)])
    def delete_entry(entry_id: str):
        # 危险项：物理删除源条目目录（含 raw/），UI 需二次确认（§4.4 v0.20 实施口径）。
        # 索引无需手动清理：删除后查询时懒同步 diff 的 removed 通道自动移除（镜像语义）。
        # 语料只扫 sources/：collection 页面天然不在本端点范围（D 类镜像不删，选项 A 拍板）；
        # curation 守卫的区域白名单（wiki/sources）确保未来语料扩展也不会误删 collections/。
        for note in _iter_notes():
            fm, _ = split_note(note.read_text(encoding="utf-8"))
            if fm.get("id") != entry_id:
                continue
            entry_rel = note.parent.relative_to(cfg.kb_root).as_posix()
            guard.remove_tree("curation", entry_rel)
            return {"deleted": True, "entry_id": entry_id}
        raise HTTPException(status_code=404, detail=f"entry not found: {entry_id}")

    @app.delete("/api/inbox/{entry_id}", dependencies=[Depends(auth)])
    def delete_inbox_entry(entry_id: str):
        # inbox 丢弃通道（§4.4 v0.21）：物理删除 inbox/<id>/ 整目录（capture.json + 原始 payload）。
        # 与"重跑复活"并列的废数据出口：error 永久不可修复或误投递时使用，UI 需二次确认。
        # id 字符白名单防路径注入（.. 等由守卫 _check 二次拦截）。
        if not re.fullmatch(r"[\w-]+", entry_id):
            raise HTTPException(status_code=400, detail=f"invalid inbox entry id: {entry_id}")
        rel = f"inbox/{entry_id}"
        if not guard.exists(rel):
            raise HTTPException(status_code=404, detail=f"inbox entry not found: {entry_id}")
        guard.remove_tree("curation", rel)
        return {"deleted": True, "entry_id": entry_id}

    @app.post("/api/kb/reset", dependencies=[Depends(auth)])
    def kb_reset(req: ResetRequest):
        """库分区重置（§4.4 v0.44）：物理删除指定分区并重建空目录。

        regions 为四区非空子集且不允许全选（全量重置含 index.db，进程内被
        SQLite 连接持有无法删除，由 scripts/reset_kb.ps1 承担）；confirm 必须
        为 "RESET" 显式确认；backup=true 重置前整库备份（排除 index.db）。
        index.db 不删：懒同步 removed 通道自动对齐（铁律 1）。
        """
        regions = sorted(set(req.regions))
        invalid = [r for r in regions if r not in KB_REGIONS]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"unknown region(s): {', '.join(invalid)}. valid: {'/'.join(KB_REGIONS)}",
            )
        if not regions:
            raise HTTPException(status_code=400, detail="regions must not be empty")
        if len(regions) == len(KB_REGIONS):
            raise HTTPException(
                status_code=400,
                detail="full reset is not allowed via API; use scripts/reset_kb.ps1",
            )
        if req.confirm != "RESET":
            raise HTTPException(status_code=400, detail='confirm must be "RESET"')
        try:
            return reset_regions(guard, regions, backup=req.backup)
        except WriteBoundaryError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/enrich/run", dependencies=[Depends(auth)])
    def enrich_run(entry_id: str | None = None):
        """手动整理（§5.1 v0.29）：带 entry_id = 单条；不带 = 同步跑一批并返回逐条 outcome。"""
        result = orch.enrich_run(entry_id)
        if not result.get("enabled", True):
            raise HTTPException(status_code=409, detail="AI enrich is disabled in config (ai.enabled)")
        return result

    @app.post("/api/enrich/preview", dependencies=[Depends(auth)])
    def enrich_preview(entry_id: str):
        """试跑（dry-run，**零落盘**）：返回解析后标签/卡片/置信度/实际后端/耗时。

        不写 wiki/、不改源条目、不计重试、不写流水（§5.1 v0.29）。
        受 ai.enabled 总闸约束；不受 trigger_mode 与熔断门控。
        """
        if not orch.ai_enabled():
            raise HTTPException(status_code=409, detail="AI enrich is disabled in config (ai.enabled)")
        try:
            return orch.enrich_preview(entry_id)
        except EntryNotFound:
            raise HTTPException(status_code=404, detail=f"entry not found: {entry_id}")
        except EntryStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except EnrichError as exc:
            # 模型不可达/超时/返回非 JSON：上游失败语义，不泄露凭据（消息已由 llm 层掩码）
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/enrich/breaker/reset", dependencies=[Depends(auth)])
    def enrich_breaker_reset():
        """显式恢复自动整理（清零熔断运行态，§5.1 v0.29）——唯一恢复路径。"""
        return {"breaker": orch.breaker_reset()}

    @app.get("/api/search", dependencies=[Depends(auth)])
    def search(q: str, limit: int = 20, mode: str = "fulltext"):
        # P4 实施口径：查询时懒同步（TTL 限频），检索结果始终对齐当前库文件；
        # mode=semantic 走向量 KNN（§5.1 v0.17），未启用/口径漂移/嵌入失败 → 409
        if mode == "semantic":
            try:
                return indexer.search_semantic(q, limit=limit)
            except SemanticSearchError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
        try:
            return indexer.search(q, limit=limit)
        except sqlite3.Error as exc:
            # 索引库异常（占用/损坏/磁盘错误）：503 + 可读原因与修复入口，不裸 500（v0.36）
            raise HTTPException(
                status_code=503,
                detail=f"index unavailable: {type(exc).__name__}: {exc}; rebuild via POST /api/index/rebuild",
            )

    @app.post("/api/index/rebuild", dependencies=[Depends(auth)])
    def rebuild_index():
        # 危险项：全量重建（向量开启时含全量重嵌，涉外部费用），UI 需二次确认
        return indexer.rebuild()

    @app.get("/api/index/status", dependencies=[Depends(auth)])
    def index_status():
        return indexer.index_status()

    @app.get("/api/config", dependencies=[Depends(auth)])
    def get_config():
        return {"config": cfg.masked()}

    @app.put("/api/config", dependencies=[Depends(auth)])
    def put_config(req: ConfigUpdateRequest):
        try:
            restart_required = cfg.apply_update(req.config)
        except ValueError as exc:
            # 枚举校验（如 ai.trigger_mode）失败：400 明确报错，不静默写入非法值
            raise HTTPException(status_code=400, detail=str(exc))
        cfg.save()
        return {"config": cfg.masked(), "restart_required": restart_required}

    # ---------- 工作台与人工处置 API（P5，§6 v0.17） ----------

    @app.get("/api/wiki", dependencies=[Depends(auth)])
    def wiki_list(status: str | None = None):
        cards = curation.list_cards(guard, status=status)
        return {"cards": cards, "total": len(cards)}

    @app.get("/api/wiki/{card_id}", dependencies=[Depends(auth)])
    def wiki_detail(card_id: str):
        card = curation.get_card(guard, card_id)
        if card is None:
            raise HTTPException(status_code=404, detail=f"wiki card not found: {card_id}")
        return card

    @app.put("/api/wiki/{card_id}", dependencies=[Depends(auth)])
    def wiki_edit(card_id: str, req: dict):
        # 修订 draft 卡正文（§4.5 v0.35，edit-before-accept）：走 curation 守卫写 wiki/
        try:
            return curation.edit_card(guard, card_id, str(req.get("body") or ""))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"wiki card not found: {card_id}")
        except curation.CardEditError as exc:
            # 非 draft / 正文为空：语义冲突或参数错，不泄露内部
            code = 400 if "empty" in str(exc) else 409
            raise HTTPException(status_code=code, detail=str(exc))

    @app.post("/api/wiki/{card_id}/promote", dependencies=[Depends(auth)])
    def wiki_promote(card_id: str):
        # 三处置之一（§4.5 晋升机制）：仅人工触发，无自动晋升通道
        if not curation.promote_card(guard, card_id):
            raise HTTPException(status_code=409, detail=f"card not found or not draft: {card_id}")
        return {"promoted": True, "card_id": card_id}

    @app.post("/api/wiki/{card_id}/regenerate", dependencies=[Depends(auth)])
    def wiki_regenerate(card_id: str):
        # 打回重生成：删卡 + 源条目复位 normalized（enrich worker 随后自动重加工）
        try:
            return curation.regenerate_card(guard, card_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"wiki card not found: {card_id}")

    @app.post("/api/wiki/{card_id}/delete", dependencies=[Depends(auth)])
    def wiki_delete(card_id: str):
        # 危险项：物理删除卡片，UI 需二次确认
        try:
            curation.delete_card(guard, card_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"wiki card not found: {card_id}")
        return {"deleted": True, "card_id": card_id}

    @app.get("/api/enrich/logs", dependencies=[Depends(auth)])
    def enrich_logs_route(limit: int = 50):
        return {"logs": curation.enrich_logs(guard, limit=limit)}

    @app.get("/api/parsers", dependencies=[Depends(auth)])
    def parsers():
        return {"parsers": list_parsers()}

    @app.post("/api/parsers/probe", dependencies=[Depends(auth)])
    def parsers_probe(req: dict):
        # 站点适配器探测（§11.5 v0.22 连接向导）：URL → 匹配 adapter + 提取站点参数
        # + 可选目录树实测验证（一次列表请求）。未识别站点 → matched=False（需先开发 adapter）。
        url = (req.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        verify = bool(req.get("verify", True))
        return probe_site(url, verify=verify)

    @app.get("/api/collections/{collection_id}/toc", dependencies=[Depends(auth)])
    def collection_toc(collection_id: str):
        # 三态裁决（v0.23）：collection 不存在 → 404；已注册但首抓未完成（toc.json 未生成）→ 409；
        # 正常 → 目录树快照。区分避免注册后前端立即拉取目录树误报"collection not found"。
        if orch.sync_engine.get_collection(collection_id) is None:
            raise HTTPException(status_code=404, detail=f"collection not found: {collection_id}")
        toc = orch.sync_engine.get_toc(collection_id)
        if toc is None:
            raise HTTPException(status_code=409, detail="首抓进行中：目录树快照尚未生成，请稍后刷新")
        return toc

    @app.get("/api/collections/{collection_id}/image", dependencies=[Depends(auth)])
    def collection_image(collection_id: str, path: str, name: str):
        """collection 图片只读端点（§6 v0.31，docx 嵌图取图通道，不涉写边界）。

        仅放行 docs/<path>/raw/img-*：name 必须 img- 前缀且不含路径分隔符，
        拼接后 resolve 校验必须位于 docs 根之下（防路径穿越）。
        """
        if orch.sync_engine.get_collection(collection_id) is None:
            raise HTTPException(status_code=404, detail=f"collection not found: {collection_id}")
        if not name.startswith("img-") or "/" in name or "\\" in name or ".." in name:
            raise HTTPException(status_code=422, detail=f"invalid image name: {name}")
        docs_root = (cfg.kb_root / "collections" / collection_id / "docs").resolve()
        target = docs_root.joinpath(*path.split("/"), "raw", name).resolve()
        if not target.is_relative_to(docs_root) or not target.is_file():
            raise HTTPException(status_code=404, detail=f"image not found: {path}/{name}")
        media_type = _IMAGE_MEDIA.get(target.suffix.lower(), "application/octet-stream")
        return Response(content=target.read_bytes(), media_type=media_type)

    @app.get("/api/collections/{collection_id}/export", dependencies=[Depends(auth)])
    def collection_export(collection_id: str, format: str = "zip", images: str = "original"):
        """全文导出（§6 v0.30，v0.32 定稿，只读派生、不涉写边界）。

        zip = 中文标题布局（标题命名 md + 目录 raw 图片 + toc.md 索引）；
        merged = 单文件全文（images=original 默认：图片按 meta.json 映射改写回原站
        URL，无映射回落 image API URL；relative 相对引用；api 全部走 image 端点）；
        pages = 全量页面 JSON（zip_path 后端统一计算），前端逐页转 docx 打包的数据源。
        error 页（抓取/归一化失败，不在 toc.json）按 sync.errors 占位标注"该页抓取失败"。
        """
        coll = orch.sync_engine.get_collection(collection_id)
        if coll is None:
            raise HTTPException(status_code=404, detail=f"collection not found: {collection_id}")
        toc = orch.sync_engine.get_toc(collection_id)
        if toc is None:
            raise HTTPException(status_code=409, detail="首抓进行中：目录树快照尚未生成，请稍后刷新")
        sync_errors = (coll.get("sync") or {}).get("errors") or []
        docs_root = cfg.kb_root / "collections" / collection_id / "docs"

        def read_note(path: str) -> str | None:
            # 导出只读，不施加写边界；路径由 toc.json/sync.errors 供给（均为系统自身产物）
            p = docs_root.joinpath(*path.split("/"), "note.md")
            return p.read_text(encoding="utf-8") if p.exists() else None

        def list_images(path: str) -> list[str]:
            raw = docs_root.joinpath(*path.split("/"), "raw")
            return sorted(f.name for f in raw.iterdir() if f.is_file() and f.name.startswith("img-")) if raw.exists() else []

        def image_bytes(path: str, name: str) -> bytes:
            return docs_root.joinpath(*path.split("/"), "raw", name).read_bytes()

        def image_url(page_path: str, rel: str) -> str:
            # raw/img-<sha1>.<ext> → image 端点 URL（query 参数整体 URL 编码，避免 %2F 路由问题）
            return f"/api/collections/{collection_id}/image?path={quote(page_path, safe='')}&name={quote(rel.split('/')[-1], safe='')}"

        meta_cache: dict[str, dict] = {}

        def image_map(page_path: str, img_name: str) -> str | None:
            # meta.json raw_files 图片条目（v0.32：{path, src}）→ 原站图片 URL；str 条目/缺失为未映射
            if page_path not in meta_cache:
                mp = docs_root.joinpath(*page_path.split("/"), "meta.json")
                try:
                    meta_cache[page_path] = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
                except Exception:
                    meta_cache[page_path] = {}
            for entry in (meta_cache[page_path].get("raw_files") or []):
                if isinstance(entry, dict) and str(entry.get("path", "")).endswith(f"/{img_name}"):
                    return entry.get("src") or None
            return None

        date_tag = exporter.datetime.now().strftime("%Y%m%d")
        if format == "merged":
            if images not in ("original", "relative", "api"):
                raise HTTPException(status_code=422, detail=f"unsupported images: {images}（original | relative | api）")
            md = exporter.build_merged_markdown(
                toc, coll, sync_errors, read_note, image_mode=images, image_map=image_map, image_url=image_url
            )
            return Response(
                content=md,
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="full-{collection_id}-{date_tag}.md"'},
            )
        if format == "zip":
            data = exporter.build_zip(toc, coll, sync_errors, read_note, list_images, image_bytes)
            return Response(
                content=data,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="export-{collection_id}-{date_tag}.zip"'},
            )
        if format == "pages":
            # docx 打包变体数据源：zip_path 后端统一计算（单一命名事实源），toc_md 链接为 .docx
            return exporter.build_pages_payload(toc, coll, sync_errors, read_note, ext="docx")
        raise HTTPException(status_code=422, detail=f"unsupported format: {format}（zip | merged | pages）")

    def _iter_all_notes():
        for region in ("sources", "collections"):
            root = cfg.kb_root / region
            if not root.exists():
                continue
            yield from sorted(root.rglob("note.md"))

    @app.get("/api/facets", dependencies=[Depends(auth)])
    def facets():
        # 过滤器选项来自后端（UI 规则：platform 开放枚举 = 注册表 ∪ 库内实际值）
        observed_platforms: set[str] = set()
        observed_types: set[str] = set()
        for note in _iter_all_notes():
            fm, _ = split_note(note.read_text(encoding="utf-8"))
            if fm.get("platform"):
                observed_platforms.add(str(fm["platform"]))
            if fm.get("source_type"):
                observed_types.add(str(fm["source_type"]))
        return {
            # "web" 为兜底平台值（§4.2），恒在选项中
            "platforms": sorted(set(platform_options()) | {"web"} | observed_platforms),
            "source_types": sorted(set(source_type_options()) | observed_types),
        }

    # ---------- Web UI 静态托管（P5：webui/ 构建产物，无产物时不挂载） ----------

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/app", SPAStaticFiles(directory=static_dir, html=True), name="webui")

    return app


class SPAStaticFiles(StaticFiles):
    """SPA 回退托管：前端路由路径（如 /app/settings）不存在对应文件时回落 index.html。

    Starlette 对缺失路径抛 HTTPException(404)（html 模式无 404.html 时），须捕获后回落。
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # 注意：StaticFiles 抛的是 starlette.HTTPException 基类，
            # except fastapi.HTTPException（子类）匹配不到它
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise
