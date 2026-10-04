"""管道编排器（§11.4 调度台）：状态机 inbox → normalized / error，及 enrich 推进。

服务启动扫描恢复现场；error 条目记录 error_stage/error_message 并保持可重试。
inbox 阶段状态载体为 capture.json sidecar（§4.2），故 inbox 期 error 详情也
记于 sidecar；normalize 产出的条目状态迁移到 frontmatter。

enrich（§5.1 实施口径）：独立线程周期扫描 status: normalized 的源条目，
调用 AI 整理引擎；重试计数记 meta.json enrich.attempts，超限转 error
（error_stage: enrich），成功置 status: enriched。失败不阻塞 normalize 主链路。

触发门控（v0.29）：线程每轮醒来先过 `ai.trigger_mode`（manual 默认不自动扫）
与熔断状态（`ai.breaker.state=open` 时静默停止），通过后才执行一批；
手动 run/preview 不经门控。"开了总闸"不等于"自动全量跑"。
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .config import Config
from .enrich import EnrichError, Enricher, EntryNotFound, EntryStateError
from .frontmatter import split_note
from .guard import Guard
from .normalize import FetchFn, NormalizeError, http_fetch, normalize_entry
from .sync import SyncEngine

ARCHIVE_DIR = "_archived"

ENRICH_STATUS_WHITELIST = {"status"}

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        cfg: Config,
        guard: Guard,
        fetcher: FetchFn | None = None,
        enricher: Enricher | None = None,
        playwright_fetcher: FetchFn | None = None,
    ):
        self.cfg = cfg
        self.guard = guard
        if fetcher is None:
            timeout = float(cfg.data.get("normalize", {}).get("fetch_timeout", 20.0))
            fetcher = lambda u: http_fetch(u, timeout=timeout)  # noqa: E731
        self.fetcher = fetcher
        self.playwright_fetcher = playwright_fetcher  # 动态页兜底注入口（测试/自定义实现）
        self.enricher = enricher or Enricher(cfg, guard)
        self.sync_engine = SyncEngine(cfg, guard)
        self.last_scan: str | None = None
        self.last_enrich_scan: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._enrich_stop = threading.Event()
        self._enrich_thread: threading.Thread | None = None
        self._enrich_wake = threading.Event()
        # D 类同步（§5.2 v0.16）：无周期线程，仅 API 手动下发时唤醒执行
        self._sync_stop = threading.Event()
        self._sync_thread: threading.Thread | None = None
        self._sync_wake = threading.Event()
        self._sync_pending: set[str] = set()

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
        self._enrich_thread = threading.Thread(target=self._enrich_loop, daemon=True)
        self._enrich_thread.start()
        if self._sync_thread and self._sync_thread.is_alive():
            return
        self._sync_stop.clear()
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._sync_thread.start()

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
        self._sync_stop.set()
        self._sync_wake.set()
        if self._sync_thread:
            self._sync_thread.join(timeout=5)
            self._sync_thread = None

    def _loop(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            self._stop.wait(interval)

    def _enrich_loop(self) -> None:
        """enrich worker 线程：每轮醒来先过门控（§5.1 v0.29）。

        `ai.poll_interval` 每轮实时读（此前启动时读进线程，改配置必须重启服务）；
        `ai.trigger_mode` 非 auto（或熔断已 open）时醒来不做任何事——
        "开了 ai.enabled" 不等于"自动全量跑"，试跑/单条/批量均走手动指令。
        """
        while not self._enrich_stop.is_set():
            interval = self._ai_poll_interval()
            self._enrich_wake.wait(interval)  # 周期醒来；手动触发可立即打断空等
            self._enrich_wake.clear()
            if self._enrich_stop.is_set():
                break
            try:
                self.enrich_auto_once()
            except Exception:
                pass

    # ---------- D 类同步（§5.2 sync 实施口径：手动下发，无周期线程） ----------

    def _sync_loop(self) -> None:
        while not self._sync_stop.is_set():
            self._sync_wake.wait()  # 挂起等待手动下发；stop 时由事件打断
            self._sync_wake.clear()
            if self._sync_stop.is_set():
                break
            with self._lock:
                pending = sorted(self._sync_pending)
                self._sync_pending.clear()
            for collection_id in pending:
                if self._sync_stop.is_set():
                    break
                try:
                    self.sync_engine.sync(collection_id)
                except Exception:
                    pass  # 失败详情已记入 collection.json sync 状态，可再次下发重试

    def sync_enqueue(self, collection_id: str) -> dict:
        """手动下发同步指令：worker 运行中则唤醒线程执行，否则同步执行（测试/禁用 worker）。"""
        if self._sync_thread and self._sync_thread.is_alive():
            with self._lock:
                self._sync_pending.add(collection_id)
            self._sync_wake.set()
            return {"triggered": True, "mode": "async"}
        return {"triggered": True, "mode": "inline", "result": self.sync_engine.sync(collection_id)}

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
            result = normalize_entry(
                inbox_dir, self.guard, self.cfg.data, self.fetcher, self.playwright_fetcher
            )
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

    def ai_enabled(self) -> bool:
        """AI 总闸（§5.1 v0.29）：允许发起任何 LLM 调用，含试跑；与"谁来触发"无关。"""
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

    # ---------- enrich 执行原语与自动调度（§5.1 v0.29：触发与总闸解耦） ----------

    @staticmethod
    def _empty_summary(skipped_reason: str | None = None) -> dict:
        return {
            "scanned": 0,
            "enriched": 0,
            "retry": 0,
            "error": 0,
            "skipped": 0,
            "results": [],
            "skipped_reason": skipped_reason,
        }

    def _last_error_message(self, note_rel: str) -> str | None:
        """最近一次 enrich 流水里的失败原因（只读 meta.json，不写盘）。"""
        log = (self._read_meta(note_rel).get("enrich") or {}).get("log") or []
        for item in reversed(log):
            if item.get("message"):
                return str(item["message"])
        return None

    def _result_row(self, note_rel: str, fm: dict, outcome: str) -> dict:
        row = {
            "entry_id": str(fm.get("id") or ""),
            "title": str(fm.get("title") or ""),
            "outcome": outcome,
        }
        if outcome in ("retry", "error"):
            row["message"] = self._last_error_message(note_rel)
        return row

    def enrich_scan_once(self) -> dict:
        """执行一批（`ai.batch_size` 条）——**执行原语**，手动与自动路径共用。

        不做 `trigger_mode`/熔断判定：那是自动调度策略（见 `enrich_auto_once`）。
        返回逐条 outcome 汇总（`results`），供手动"跑一批"给 UI 即时反馈。
        """
        summary = self._empty_summary()
        if not self.ai_enabled():
            summary["skipped_reason"] = "ai_disabled"
            return summary
        batch_size = int(self.cfg.data.get("ai", {}).get("batch_size", 5))
        concurrency = max(1, int(self.cfg.data.get("ai", {}).get("concurrency", 1)))
        targets = self._enrich_targets()[:batch_size]
        summary["scanned"] = len(targets)
        if not targets:
            return summary
        outcomes: dict[str, str] = {}
        if concurrency == 1:
            for note_rel, _fm, _meta in targets:
                # 逐条独立收口：单条异常（含后置记账写盘失败）只影响自己，
                # 不打断整批——与并发路径同口径（§4.4 v0.34）
                try:
                    outcome = self._enrich_one(note_rel)
                except Exception:
                    logger.exception("enrich 单条执行未收口：%s", note_rel)
                    outcome = "error"
                outcomes[note_rel] = outcome
                summary[outcome] = summary.get(outcome, 0) + 1
        else:
            # 批内并发（§11.5 ai.concurrency）：逐条彼此独立（各自 meta 各自文件），无共享写
            with ThreadPoolExecutor(max_workers=min(concurrency, len(targets))) as pool:
                futures = {pool.submit(self._enrich_one, note_rel): note_rel for note_rel, _fm, _meta in targets}
                for fut in as_completed(futures):
                    try:
                        outcome = fut.result()
                    except Exception:
                        outcome = "error"
                    outcomes[futures[fut]] = outcome
                    summary[outcome] = summary.get(outcome, 0) + 1
        # 逐条汇总（顺序与扫描顺序一致；失败原因取自该条目 meta.json 流水）
        summary["results"] = [
            self._result_row(rel, fm, outcomes.get(rel, "error")) for rel, fm, _ in targets
        ]
        self.last_enrich_scan = datetime.now().astimezone().isoformat(timespec="seconds")
        return summary

    # ---------- 自动调度门控与熔断（§5.1 v0.29） ----------

    def _ai_poll_interval(self) -> float:
        """扫描间隔每轮实时读（§5.1 v0.29）；下限 1 秒防止 0/负值空转打满 CPU。"""
        raw = self.cfg.data.get("ai", {}).get("poll_interval")
        return max(1.0, float(5.0 if raw is None else raw))

    def _trigger_mode(self) -> str:
        return str(self.cfg.data.get("ai", {}).get("trigger_mode", "manual"))

    def _breaker(self) -> dict:
        return dict(self.cfg.data.get("ai", {}).get("breaker") or {})

    def _breaker_open(self) -> bool:
        return self._breaker().get("state") == "open"

    def auto_dispatch_allowed(self) -> bool:
        """自动扫描门控：总闸开 + `trigger_mode=auto` + 熔断未 open。"""
        return self.ai_enabled() and self._trigger_mode() == "auto" and not self._breaker_open()

    def enrich_auto_once(self) -> dict:
        """worker 每轮入口：门控 → 执行一批 → 熔断记账（手动路径不经过此处）。"""
        if not self.ai_enabled():
            return self._empty_summary("ai_disabled")
        if self._trigger_mode() != "auto":
            return self._empty_summary("manual_mode")
        if self._breaker_open():
            return self._empty_summary("breaker_open")
        summary = self.enrich_scan_once()
        self._update_breaker(summary)
        return summary

    def _update_breaker(self, summary: dict) -> None:
        """熔断记账：**整轮无成功**累计、任一轮有成功清零、达阈值置 open（§5.1 v0.29）。

        失败 = retry + error：模型不可达/超时/输出不可解析时，重试未超限的条目 outcome
        是 retry，所以只看 error 会漏判"整轮全失败"（实测踩过）。open 后不自动恢复
        （等显式 breaker_reset）——程序不自行判断"是否已修好"。
        """
        if self._breaker_open():
            return
        scanned = int(summary.get("scanned") or 0)
        if not scanned:
            return  # 无目标（无 normalized 条目）不参与熔断判定
        failed = int(summary.get("error") or 0) + int(summary.get("retry") or 0)
        successes = int(summary.get("enriched") or 0)
        failures = int(self._breaker().get("consecutive_failures") or 0) + 1 if failed and not successes else 0
        fields: dict = {"consecutive_failures": failures}
        threshold = max(1, int(self.cfg.data.get("ai", {}).get("breaker_threshold", 3) or 3))
        if failures >= threshold:
            first = next((r.get("message") for r in summary.get("results", []) if r.get("message")), None)
            fields.update(
                state="open",
                opened_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                last_error=str(first or "all targets failed")[:500],
            )
        self.cfg.write_breaker(**fields)

    def breaker_reset(self) -> dict:
        """显式恢复自动整理（唯一恢复路径，UI「恢复自动整理」动作）。"""
        return self.cfg.write_breaker(
            state="closed", consecutive_failures=0, opened_at=None, last_error=None
        )

    ENRICH_LOG_LIMIT = 20  # meta.json enrich.log 保留条数（程序消费的滚动流水）

    @staticmethod
    def _append_enrich_log(meta: dict, outcome: str, message: str | None, attempts: int) -> None:
        """enrich 操作流水（§4.4 v0.17）：记 meta.json enrich.log，滚动保留最近 N 条。"""
        enrich_state = dict(meta.get("enrich") or {})
        log = list(enrich_state.get("log") or [])
        entry: dict = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "outcome": outcome}
        if message:
            entry["message"] = message[:300]
        entry["attempts"] = attempts
        log.append(entry)
        enrich_state["log"] = log[-Orchestrator.ENRICH_LOG_LIMIT :]
        meta["enrich"] = enrich_state

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
        self._append_enrich_log(meta, "enriched", None, attempts)
        self._write_meta(note_rel, meta)
        self.guard.patch_note_fields("enrich", note_rel, {"status": "enriched"}, ENRICH_STATUS_WHITELIST)
        return "enriched"

    def _enrich_fail(self, note_rel: str, meta: dict, message: str) -> str:
        """记录失败并给出终态/retry 判定。

        失败态落盘本身失败（写边界拒绝、只读介质、Windows 锁耗尽重试）时
        **不向上冒泡**（§4.4 v0.34）：原实现里这次二次抛出无人接，直接变成
        HTTP 500，且铁律 3 要求的 `error` 态与 `error_stage`/`error_message`
        全部丢失（实测 D 类页整理即如此）。此时记服务端日志并按终态 `error`
        返回——日志不是静默，返回值也不是假的 retry。
        """
        max_attempts = int(self.cfg.data.get("ai", {}).get("max_attempts", 3))
        attempts = int((meta.get("enrich") or {}).get("attempts", 0))
        try:
            if attempts >= max_attempts:
                meta["error_stage"] = "enrich"
                meta["error_message"] = message[:500]
                self._append_enrich_log(meta, "error", message, attempts)
                self._write_meta(note_rel, meta)
                self.guard.patch_note_fields("enrich", note_rel, {"status": "error"}, ENRICH_STATUS_WHITELIST)
                return "error"
            self._append_enrich_log(meta, "retry", message, attempts)
            self._write_meta(note_rel, meta)
            return "retry"
        except Exception:
            logger.exception(
                "enrich 失败态落盘失败（entry=%s, attempts=%s, 原因=%s）", note_rel, attempts, message
            )
            return "error"

    def find_note_rel(self, entry_id: str) -> tuple[str, dict] | None:
        """按 frontmatter id 找条目（sources + collections 两区），返回 (note_rel, frontmatter)。"""
        for note in self._iter_notes():
            fm, _ = split_note(note.read_text(encoding="utf-8"))
            if fm.get("id") == entry_id:
                return note.relative_to(self.guard.kb_root).as_posix(), fm
        return None

    def enrich_run(self, entry_id: str | None = None) -> dict:
        """手动触发（§5.1 v0.29）：带 id = 单条同步；不带 = **同步跑一批**并返回逐条 outcome。

        手动路径不受 `trigger_mode` 与熔断门控（人触发不受自动化门控）。
        """
        if not self.ai_enabled():
            return {"enabled": False}
        if entry_id:
            found = self.find_note_rel(entry_id)
            if not found:
                return {"enabled": True, "entry_id": entry_id, "outcome": "not_found"}
            note_rel, fm = found
            if fm.get("status") not in ("normalized", "error"):
                return {"enabled": True, "entry_id": entry_id, "outcome": "skipped", "status": fm.get("status")}
            outcome = self._enrich_one(note_rel)
            return {"enabled": True, "entry_id": entry_id, "outcome": outcome, "detail": self._result_row(note_rel, fm, outcome)}
        return {"enabled": True, "batch": self.enrich_scan_once()}

    def enrich_preview(self, entry_id: str) -> dict:
        """试跑（dry-run，**零落盘**）：调模型 + 解析后返回结果，不写任何产物（§5.1 v0.29）。

        存在性与状态判定在此单一来源（API 层只做总闸门控与错误码映射）；
        手动路径不受 `trigger_mode` 与熔断门控（人触发不受自动化门控）。
        """
        found = self.find_note_rel(entry_id)
        if not found:
            raise EntryNotFound(entry_id)
        note_rel, fm = found
        if fm.get("status") not in ("normalized", "error"):
            raise EntryStateError(
                f"entry status is '{fm.get('status')}', only normalized/error can be enriched"
            )
        return self.enricher.plan_entry(note_rel)

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
            self._append_enrich_log(meta, "rerun", None, 0)
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
        # by_entry（v0.46）：按 capture.json entry 通道计数（含 error 项），总览页漏斗细分数据源
        inbox = {"inbox": 0, "error": 0, "by_entry": {}}
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
                # 通道分组（v0.46）：未知通道按原值保留，不丢计数
                entry_ch = data.get("entry") or "unknown"
                inbox["by_entry"][entry_ch] = inbox["by_entry"].get(entry_ch, 0) + 1
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

        # pending_by_platform（v0.46）：与 pending 同一遍遍历按 platform 分组，
        # 保证总览页漏斗「待整理」站细分口径与站点总数一致（/api/entries 只扫 sources/ 不含 D 类）
        enrich = {
            "pending": 0,
            "enriched": 0,
            "errors": [],
            "last_scan": self.last_enrich_scan,
            "pending_by_platform": {},
            "pending_by_region": {},
        }
        for note in self._iter_notes():
            try:
                fm, _ = split_note(note.read_text(encoding="utf-8"))
            except Exception:
                continue
            st = fm.get("status")
            if st == "normalized":
                enrich["pending"] += 1
                # platform / region 分组（v0.46/v0.48）：未知值原样保留，不丢计数；
                # region 区分集合镜像页（collections，去文档树整理）与单条沉淀（sources，时间流可见）
                plat = fm.get("platform") or "unknown"
                enrich["pending_by_platform"][plat] = enrich["pending_by_platform"].get(plat, 0) + 1
                region = note.relative_to(self.guard.kb_root).parts[0]
                enrich["pending_by_region"][region] = enrich["pending_by_region"].get(region, 0) + 1
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

        collections_summary = []
        for coll in self.sync_engine.list_collections():
            sync_state = coll.get("sync") or {}
            docs_root = kb / "collections" / str(coll.get("id")) / "docs"
            pages = len(list(docs_root.rglob("note.md"))) if docs_root.exists() else 0
            collections_summary.append(
                {
                    "id": coll.get("id"),
                    "name": coll.get("name"),
                    "entry_url": coll.get("entry_url"),  # v0.49：总览集合同步面板原站外链
                    "state": sync_state.get("state"),
                    "pages": pages,
                    "last_synced_at": sync_state.get("last_synced_at"),
                    "last_error": sync_state.get("last_error"),
                }
            )

        return {
            "inbox": inbox,
            "archived": archived,
            "errors": errors,
            "sources_entries": sources_count,
            "collections": collections_summary,
            "enrich": enrich,
            "ai_enabled": self.ai_enabled(),
            # 触发门控与熔断运行态（§5.1 v0.29）：工作台据此显示"自动/手动/已熔断"
            "ai_trigger_mode": self._trigger_mode(),
            "ai_breaker": self._breaker(),
            "guard_violations": dict(self.guard.violations),
            "last_scan": self.last_scan,
        }
