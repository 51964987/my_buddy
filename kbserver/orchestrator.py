"""管道编排器（§11.4 调度台）：状态机 inbox → normalized / error，及 enrich 推进。

服务启动扫描恢复现场；error 条目记录 error_stage/error_message 并保持可重试。
inbox 阶段状态载体为 capture.json sidecar（§4.2），故 inbox 期 error 详情也
记于 sidecar；normalize 产出的条目状态迁移到 frontmatter。

enrich（§5.1 实施口径）：独立线程周期扫描 status: normalized 的源条目，
调用 AI 整理引擎；重试计数记 meta.json enrich.attempts，超限转 error
（error_stage: enrich），成功置 status: enriched。失败不阻塞 normalize 主链路。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from .config import Config
from .enrich import EnrichError, Enricher
from .frontmatter import split_note
from .guard import Guard
from .normalize import FetchFn, NormalizeError, http_fetch, normalize_entry

ARCHIVE_DIR = "_archived"

ENRICH_STATUS_WHITELIST = {"status"}


class Orchestrator:
    def __init__(self, cfg: Config, guard: Guard, fetcher: FetchFn | None = None, enricher: Enricher | None = None):
        self.cfg = cfg
        self.guard = guard
        if fetcher is None:
            timeout = float(cfg.data.get("normalize", {}).get("fetch_timeout", 20.0))
            fetcher = lambda u: http_fetch(u, timeout=timeout)  # noqa: E731
        self.fetcher = fetcher
        self.enricher = enricher or Enricher(cfg, guard)
        self.last_scan: str | None = None
        self.last_enrich_scan: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._enrich_stop = threading.Event()
        self._enrich_thread: threading.Thread | None = None
        self._enrich_wake = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        interval = float(self.cfg.data.get("pipeline", {}).get("poll_interval", 2.0))
        self._thread = threading.Thread(target=self._loop, args=(interval,), daemon=True)
        self._thread.start()
        if self._enrich_thread and self._enrich_thread.is_alive():
            return
        self._enrich_stop.clear()
        ai_cfg = self.cfg.data.get("ai", {})
        e_interval = float(ai_cfg.get("poll_interval", 5.0))
        self._enrich_thread = threading.Thread(target=self._enrich_loop, args=(e_interval,), daemon=True)
        self._enrich_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._enrich_stop.set()
        self._enrich_wake.set()
        if self._enrich_thread:
            self._enrich_thread.join(timeout=5)
            self._enrich_thread = None

    def _loop(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            self._stop.wait(interval)

    def _enrich_loop(self, interval: float) -> None:
        while not self._enrich_stop.is_set():
            self._enrich_wake.wait(interval)  # 周期醒来；手动触发可立即打断空等
            self._enrich_wake.clear()
            if self._enrich_stop.is_set():
                break
            try:
                self.enrich_scan_once()
            except Exception:
                pass

    def _inbox_dirs(self) -> list[Path]:
        root = self.guard.kb_root / "inbox"
        if not root.exists():
            return []
        return sorted(d for d in root.iterdir() if d.is_dir() and d.name != ARCHIVE_DIR)

    def scan_once(self) -> dict:
        summary = {"processed": 0, "normalized": 0, "duplicate": 0, "updated": 0, "retry": 0, "error": 0}
        with self._lock:
            for inbox_dir in self._inbox_dirs():
                cj = inbox_dir / "capture.json"
                if not cj.exists():
                    continue
                try:
                    data = json.loads(cj.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if data.get("status") != "inbox":
                    continue
                outcome = self.process_entry(inbox_dir, data)
                summary["processed"] += 1
                summary[outcome] = summary.get(outcome, 0) + 1
        self.last_scan = datetime.now().astimezone().isoformat(timespec="seconds")
        return summary

    def process_entry(self, inbox_dir: Path, capture: dict) -> str:
        name = inbox_dir.name
        cj_rel = f"inbox/{name}/capture.json"
        try:
            result = normalize_entry(inbox_dir, self.guard, self.cfg.data, self.fetcher)
        except NormalizeError as exc:
            return self._fail(cj_rel, capture, exc.stage, exc.message)
        except Exception as exc:
            return self._fail(cj_rel, capture, "internal", f"{type(exc).__name__}: {exc}")

        if result["outcome"] == "duplicate":
            ts = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
            self.guard.move_tree("normalize", f"inbox/{name}", f"inbox/{ARCHIVE_DIR}/{name}-{ts}")
            return "duplicate"

        self.guard.remove_tree("normalize", f"inbox/{name}")
        return "updated" if result["outcome"] == "updated" else "normalized"

    def _fail(self, cj_rel: str, capture: dict, stage: str, message: str) -> str:
        max_attempts = int(self.cfg.data.get("pipeline", {}).get("max_attempts", 3))
        attempts = int(capture.get("attempts", 0)) + 1
        capture["attempts"] = attempts
        if attempts >= max_attempts:
            capture["status"] = "error"
            capture["error_stage"] = stage
            capture["error_message"] = message[:500]
            outcome = "error"
        else:
            outcome = "retry"
        self.guard.write_json("normalize", cj_rel, capture)
        return outcome

    # ---------- enrich（§5.1 实施口径） ----------

    def _ai_enabled(self) -> bool:
        return bool(self.cfg.data.get("ai", {}).get("enabled", False))

    def _iter_notes(self):
        kb = self.guard.kb_root
        for region in ("sources", "collections"):
            root = kb / region
            if root.exists():
                yield from sorted(root.rglob("note.md"))

    def _read_meta(self, note_rel: str) -> dict:
        meta_rel = note_rel.rsplit("/", 1)[0] + "/meta.json"
        path = self.guard.kb_root.joinpath(*meta_rel.split("/"))
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_meta(self, note_rel: str, meta: dict) -> None:
        meta_rel = note_rel.rsplit("/", 1)[0] + "/meta.json"
        self.guard.write_json("enrich", meta_rel, meta)

    def _enrich_targets(self) -> list[tuple[str, dict, dict]]:
        """待 enrich 条目：normalized；或 enrich 超限 error 前的重试（attempts 未达上限）。"""
        max_attempts = int(self.cfg.data.get("ai", {}).get("max_attempts", 3))
        targets = []
        for note in self._iter_notes():
            try:
                fm, _ = split_note(note.read_text(encoding="utf-8"))
            except Exception:
                continue
            status = fm.get("status")
            if status not in ("normalized", "error"):
                continue
            note_rel = note.relative_to(self.guard.kb_root).as_posix()
            meta = self._read_meta(note_rel)
            if status == "error":
                if meta.get("error_stage") != "enrich":
                    continue
                attempts = int((meta.get("enrich") or {}).get("attempts", 0))
                if attempts >= max_attempts:
                    continue
            targets.append((note_rel, fm, meta))
        return targets

    def enrich_scan_once(self) -> dict:
        summary = {"scanned": 0, "enriched": 0, "retry": 0, "error": 0, "skipped": 0}
        if not self._ai_enabled():
            return summary
        batch_size = int(self.cfg.data.get("ai", {}).get("batch_size", 5))
        for note_rel, _fm, _meta in self._enrich_targets():
            if summary["scanned"] >= batch_size:
                break
            summary["scanned"] += 1
            outcome = self._enrich_one(note_rel)
            summary[outcome] = summary.get(outcome, 0) + 1
        self.last_enrich_scan = datetime.now().astimezone().isoformat(timespec="seconds")
        return summary

    def _enrich_one(self, note_rel: str) -> str:
        meta = self._read_meta(note_rel)
        enrich_state = dict(meta.get("enrich") or {})
        attempts = int(enrich_state.get("attempts", 0)) + 1
        enrich_state["attempts"] = attempts
        meta["enrich"] = enrich_state
        try:
            self.enricher.enrich_entry(note_rel)
        except EnrichError as exc:
            return self._enrich_fail(note_rel, meta, str(exc))
        except Exception as exc:
            return self._enrich_fail(note_rel, meta, f"{type(exc).__name__}: {exc}")
        meta.pop("error_stage", None)
        meta.pop("error_message", None)
        self._write_meta(note_rel, meta)
        self.guard.patch_note_fields("enrich", note_rel, {"status": "enriched"}, ENRICH_STATUS_WHITELIST)
        return "enriched"

    def _enrich_fail(self, note_rel: str, meta: dict, message: str) -> str:
        max_attempts = int(self.cfg.data.get("ai", {}).get("max_attempts", 3))
        attempts = int((meta.get("enrich") or {}).get("attempts", 0))
        if attempts >= max_attempts:
            meta["error_stage"] = "enrich"
            meta["error_message"] = message[:500]
            self._write_meta(note_rel, meta)
            self.guard.patch_note_fields("enrich", note_rel, {"status": "error"}, ENRICH_STATUS_WHITELIST)
            return "error"
        self._write_meta(note_rel, meta)
        return "retry"

    def enrich_run(self, entry_id: str | None = None) -> dict:
        """手动触发：指定条目同步执行；否则唤醒 worker 扫描。"""
        if not self._ai_enabled():
            return {"enabled": False}
        if entry_id:
            for note in self._iter_notes():
                fm, _ = split_note(note.read_text(encoding="utf-8"))
                if fm.get("id") != entry_id:
                    continue
                note_rel = note.relative_to(self.guard.kb_root).as_posix()
                if fm.get("status") not in ("normalized", "error"):
                    return {"enabled": True, "entry_id": entry_id, "outcome": "skipped", "status": fm.get("status")}
                return {"enabled": True, "entry_id": entry_id, "outcome": self._enrich_one(note_rel)}
            return {"enabled": True, "entry_id": entry_id, "outcome": "not_found"}
        self._enrich_wake.set()
        return {"enabled": True, "triggered": True, "pending": len(self._enrich_targets())}

    def rerun_enrich(self, entry_id: str) -> bool:
        """enrich error 条目复活：重置回 normalized 并清零重试计数。"""
        for note in self._iter_notes():
            fm, _ = split_note(note.read_text(encoding="utf-8"))
            if fm.get("id") != entry_id:
                continue
            note_rel = note.relative_to(self.guard.kb_root).as_posix()
            meta = self._read_meta(note_rel)
            if fm.get("status") != "error" or meta.get("error_stage") != "enrich":
                return False
            meta.pop("error_stage", None)
            meta.pop("error_message", None)
            meta["enrich"] = {**(meta.get("enrich") or {}), "attempts": 0}
            self._write_meta(note_rel, meta)
            self.guard.patch_note_fields("enrich", note_rel, {"status": "normalized"}, ENRICH_STATUS_WHITELIST)
            return True
        return False

    def rerun(self, entry_id: str) -> bool:
        cj_rel = f"inbox/{entry_id}/capture.json"
        path = self.guard.kb_root.joinpath(*cj_rel.split("/"))
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") != "error":
            return False
        data["status"] = "inbox"
        data["attempts"] = 0
        data.pop("error_stage", None)
        data.pop("error_message", None)
        self.guard.write_json("normalize", cj_rel, data)
        return True

    def status(self) -> dict:
        kb = self.guard.kb_root
        inbox = {"inbox": 0, "error": 0}
        errors: list[dict] = []
        archived = 0
        root = kb / "inbox"
        if root.exists():
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                if d.name == ARCHIVE_DIR:
                    archived += sum(1 for x in d.iterdir() if x.is_dir())
                    continue
                cj = d / "capture.json"
                if not cj.exists():
                    continue
                try:
                    data = json.loads(cj.read_text(encoding="utf-8"))
                except Exception:
                    continue
                st = data.get("status")
                if st in inbox:
                    inbox[st] += 1
                if st == "error":
                    errors.append(
                        {
                            "id": d.name,
                            "entry": data.get("entry"),
                            "url": data.get("url"),
                            "error_stage": data.get("error_stage"),
                            "error_message": data.get("error_message"),
                        }
                    )
        sources_count = len(list((kb / "sources").rglob("note.md"))) if (kb / "sources").exists() else 0

        enrich = {"pending": 0, "enriched": 0, "errors": [], "last_scan": self.last_enrich_scan}
        for note in self._iter_notes():
            try:
                fm, _ = split_note(note.read_text(encoding="utf-8"))
            except Exception:
                continue
            st = fm.get("status")
            if st == "normalized":
                enrich["pending"] += 1
            elif st == "enriched":
                enrich["enriched"] += 1
            elif st == "error":
                note_rel = note.relative_to(self.guard.kb_root).as_posix()
                meta = self._read_meta(note_rel)
                if meta.get("error_stage") == "enrich":
                    enrich["errors"].append(
                        {
                            "id": fm.get("id"),
                            "title": fm.get("title"),
                            "attempts": (meta.get("enrich") or {}).get("attempts", 0),
                            "error_message": meta.get("error_message"),
                        }
                    )

        return {
            "inbox": inbox,
            "archived": archived,
            "errors": errors,
            "sources_entries": sources_count,
            "enrich": enrich,
            "ai_enabled": self._ai_enabled(),
            "guard_violations": dict(self.guard.violations),
            "last_scan": self.last_scan,
        }
