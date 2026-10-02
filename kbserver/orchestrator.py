"""管道编排器（§11.4 调度台）：状态机 inbox → normalized / error。

服务启动扫描恢复现场；error 条目记录 error_stage/error_message 并保持可重试。
inbox 阶段状态载体为 capture.json sidecar（§4.2），故 inbox 期 error 详情也
记于 sidecar；normalize 产出的条目状态迁移到 frontmatter。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from .config import Config
from .guard import Guard
from .normalize import FetchFn, NormalizeError, http_fetch, normalize_entry

ARCHIVE_DIR = "_archived"


class Orchestrator:
    def __init__(self, cfg: Config, guard: Guard, fetcher: FetchFn | None = None):
        self.cfg = cfg
        self.guard = guard
        if fetcher is None:
            timeout = float(cfg.data.get("normalize", {}).get("fetch_timeout", 20.0))
            fetcher = lambda u: http_fetch(u, timeout=timeout)  # noqa: E731
        self.fetcher = fetcher
        self.last_scan: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        interval = float(self.cfg.data.get("pipeline", {}).get("poll_interval", 2.0))
        self._thread = threading.Thread(target=self._loop, args=(interval,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            self._stop.wait(interval)

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
        return {
            "inbox": inbox,
            "archived": archived,
            "errors": errors,
            "sources_entries": sources_count,
            "guard_violations": dict(self.guard.violations),
            "last_scan": self.last_scan,
        }
