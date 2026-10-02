"""API 网关（§11.1）：Capture / Query·运维 / 参数设置 三组路由（Collection 任务 API 随 P2 落地）。

token 鉴权；Query·运维 API 含检索（P4 起，索引引擎懒同步）与索引重建指令；写操作只转交守卫。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import __version__
from .capture import CaptureError, accept_capture
from .config import Config
from .frontmatter import split_note
from .guard import Guard
from .indexer import Indexer
from .orchestrator import Orchestrator

KB_REGIONS = ("inbox", "sources", "collections", "wiki")


class CaptureRequest(BaseModel):
    url: str | None = None
    title: str | None = None
    text: str | None = None
    selected_text: str | None = None
    screenshot_b64: str | None = None
    entry: str = "cli"


class ConfigUpdateRequest(BaseModel):
    config: dict[str, Any]


def create_app(cfg: Config | None = None, orchestrator: Orchestrator | None = None) -> FastAPI:
    cfg = cfg or Config()
    guard = Guard(cfg.kb_root)
    orch = orchestrator or Orchestrator(cfg, guard)
    indexer = Indexer(cfg, guard)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
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

    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        result = orch.status()
        result["version"] = __version__
        return result

    def _iter_notes():
        sources = cfg.kb_root / "sources"
        if not sources.exists():
            return
        yield from sorted(sources.rglob("note.md"))

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
        for note in _iter_notes():
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

    @app.post("/api/enrich/run", dependencies=[Depends(auth)])
    def enrich_run(entry_id: str | None = None):
        result = orch.enrich_run(entry_id)
        if not result.get("enabled", True):
            raise HTTPException(status_code=409, detail="AI enrich is disabled in config (ai.enabled)")
        return result

    @app.get("/api/search", dependencies=[Depends(auth)])
    def search(q: str, limit: int = 20):
        # P4 实施口径：查询时懒同步（TTL 限频），检索结果始终对齐当前库文件
        return indexer.search(q, limit=limit)

    @app.post("/api/index/rebuild", dependencies=[Depends(auth)])
    def rebuild_index():
        return indexer.rebuild()

    @app.get("/api/config", dependencies=[Depends(auth)])
    def get_config():
        return {"config": cfg.masked()}

    @app.put("/api/config", dependencies=[Depends(auth)])
    def put_config(req: ConfigUpdateRequest):
        restart_required = cfg.apply_update(req.config)
        cfg.save()
        return {"config": cfg.masked(), "restart_required": restart_required}

    return app
