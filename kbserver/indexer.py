"""索引引擎（§11.4 图书馆编目员）：FTS5 全文索引（index.db），v0.15 P4 实施口径。

职责与边界：
- **只读 kb/ 库文件**：语料 = sources/collections 下 status ∈ {normalized, enriched}
  的 note.md 与 wiki/ 全部卡片（frontmatter 剥离后标题 + tags + 正文入索引；
  error 条目走状态页呈现，不入检索）；
- **唯一写入物 = kb 根目录 index.db**（可随时删除重建的派生库），路径经写边界
  守卫（index 阶段 → index.db）解析授权；SQLite 直接读写该文件即守卫授权通道；
- **中文分词（§9 决策 5，v0.15 定稿）**：jieba 搜索引擎模式预分词，索引与查询共用
  同一 segment 实现（自 txxy_test 生产验证实现迁移，含 MATCH 引号转义、空产物
  不拼 MATCH、分词器版本入库防混存等已踩坑处理）；
- **查询时懒同步**：无独立后台线程；检索 API 每次调用前做按文件（rel + mtime_ns +
  size）增量 diff（TTL 限频），分词/语料口径不一致自动全量重建；
  全量重建另经 POST /api/index/rebuild 指令下发。

查询语法安全（迁移自 txxy_test 修订 33/34）：segment 产物逐 token `"..."` 引号包裹、
token 内 `"` 转义为 `""`（用户关键词含 `"` `(` `)` `^` `-` 及 AND/OR/NOT/NEAR 等
保留词时裸拼会 OperationalError 或改变语义）；空产物直接返回空结果、不拼 MATCH
（空 MATCH 串会 OperationalError）。
"""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .frontmatter import split_note
from .guard import Guard

# 语料文本入索引库的截断上限（控制 index.db 体积；仅影响摘要定位与索引体积）
MAX_RAW_CHARS = 20000

# 懒同步限频（秒）：窗口内重复检索不重复扫盘
SYNC_TTL = 5.0

# 纯符号 / 空白 token 过滤（segment 产物为空时不拼 MATCH）
_PURE_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
    rel TEXT PRIMARY KEY, kind TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    text_seg, text_raw UNINDEXED, rel UNINDEXED, kind UNINDEXED,
    entry_id UNINDEXED, title UNINDEXED, url UNINDEXED,
    date UNINDEXED, tags UNINDEXED, status UNINDEXED
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def jieba_cut(text: str) -> list[str]:
    """jieba 搜索引擎模式薄封装：集中一处，便于未来换分词器 / 自定义词典。"""
    import jieba  # 延迟导入：词典加载约 1 秒，首次检索/建索引时才付成本

    return list(jieba.cut_for_search(text))


def segment(text: str) -> list[str]:
    """jieba 预分词：索引与查询共用的唯一分词实现，口径单一。

    - 搜索引擎模式（cut_for_search）：对长词额外切出子词（「摩托车」→「摩托」+
      「摩托车」），否则查询「摩托」匹配不到只含整词「摩托车」的文档、召回静默缺失
      （txxy_test 生产实测坑）；
    - 小写归一（仅影响 ASCII，CJK 不变）；保序去重（AND 语义下去重无损）；
    - 过滤纯符号 / 空白 token（含查询侧语法字符输入，如单独的 `"` `(`）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for tok in jieba_cut(text or ""):
        tok = tok.strip().lower()
        if not tok or _PURE_PUNCT_RE.match(tok) or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _segmenter_id() -> str:
    """分词 / 语料口径标识：jieba 版本 + 切分模式 + 语料版本（入 meta，任一变化整库重建）。"""
    import jieba

    return f"jieba{jieba.__version__}+cfs+corpus1"


def build_match_query(tokens: list[str]) -> str:
    """segment() 产物 → FTS5 MATCH 表达式（唯一构造实现）。

    逐 token `"..."` 引号包裹、token 内 `"` 转义为 `""`；空格连接 = 隐式 AND。
    调用方必须先判空（空 MATCH 串会 OperationalError）。"""
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _first_heading(body: str) -> str:
    """从正文提取首个一级标题作为无 title 元数据的文档标题（wiki 卡题目在正文里）。"""
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


class Indexer:
    def __init__(self, cfg, guard: Guard):
        self.cfg = cfg
        self.guard = guard
        # index 阶段授权区域即 index.db：路径解析经守卫（越界会拒绝并计数）
        self.db_path: Path = guard.resolve("index", "index.db")
        self._last_sync: float = 0.0

    # ---------- 语料扫描 ----------

    def _iter_corpus(self) -> list[tuple[str, dict]]:
        """扫描库文件 → [(rel, doc)]。doc 含索引元数据与 raw 文本。

        rel 为 kb 根相对 POSIX 路径，作为 docs/fts 表主键（条目移动 = 删旧插新，
        天然跟随文件系统事实源）。"""
        kb = self.guard.kb_root
        out: list[tuple[str, dict]] = []

        # 源条目：sources/ 与 collections/ 下的 note.md，只收 normalized/enriched
        for region in ("sources", "collections"):
            root = kb / region
            if not root.is_dir():
                continue
            for note in sorted(root.rglob("note.md")):
                rel = note.relative_to(kb).as_posix()
                try:
                    fm, body = split_note(note.read_text(encoding="utf-8"))
                except Exception:
                    continue  # 读取失败（人工编辑中/权限）不阻塞其余语料，下次同步重试
                if fm.get("status") not in ("normalized", "enriched"):
                    continue
                tags = fm.get("tags") or []
                out.append(
                    (
                        rel,
                        {
                            "kind": "entry",
                            "entry_id": str(fm.get("id") or ""),
                            "title": str(fm.get("title") or ""),
                            "url": str(fm.get("url") or ""),
                            "date": str(fm.get("captured_at") or ""),
                            "tags": " ".join(str(t) for t in tags),
                            "status": str(fm.get("status") or ""),
                            "text_raw": body[:MAX_RAW_CHARS],
                        },
                    )
                )

        # wiki 卡：全部收录（draft 可检索，status 列暴露供消费端过滤）
        wiki = kb / "wiki"
        if wiki.is_dir():
            for card in sorted(wiki.rglob("*.md")):
                rel = card.relative_to(kb).as_posix()
                try:
                    fm, body = split_note(card.read_text(encoding="utf-8"))
                except Exception:
                    continue
                tags = fm.get("tags") or []
                out.append(
                    (
                        rel,
                        {
                            "kind": "wiki",
                            "entry_id": str(fm.get("id") or ""),
                            "title": str(fm.get("title") or _first_heading(body)),
                            "url": "",
                            "date": str(fm.get("created_at") or ""),
                            "tags": " ".join(str(t) for t in tags),
                            "status": str(fm.get("status") or ""),
                            "text_raw": body[:MAX_RAW_CHARS],
                        },
                    )
                )
        return out

    # ---------- 连接与 schema ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_SCHEMA)
        return conn

    def _meta_get(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _meta_set(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def _insert_doc(self, conn: sqlite3.Connection, rel: str, stat: object, doc: dict) -> None:
        seg_input = f"{doc['title']}\n{doc['tags']}\n{doc['text_raw']}"
        # FTS5 对 INSERT OR REPLACE 不可靠（实测内容表残留旧文本、新词不进倒排索引），
        # 必须显式 DELETE 后 INSERT；_delete_doc 幂等，首次插入时删零行
        self._delete_doc(conn, rel)
        conn.execute(
            "INSERT OR REPLACE INTO fts(text_seg, text_raw, rel, kind, entry_id, title, url, date, tags, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                " ".join(segment(seg_input)),
                doc["text_raw"],
                rel,
                doc["kind"],
                doc["entry_id"],
                doc["title"],
                doc["url"],
                doc["date"],
                doc["tags"],
                doc["status"],
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO docs(rel, kind, mtime_ns, size) VALUES (?, ?, ?, ?)",
            (rel, doc["kind"], stat.st_mtime_ns, stat.st_size),
        )

    def _delete_doc(self, conn: sqlite3.Connection, rel: str) -> None:
        conn.execute("DELETE FROM fts WHERE rel = ?", (rel,))
        conn.execute("DELETE FROM docs WHERE rel = ?", (rel,))

    # ---------- 同步与重建 ----------

    def sync(self) -> dict:
        """口径检查 + 按文件增量 diff：新增/内容变化 → 重插，消失 → 删除。返回增量摘要。"""
        summary = {"added": 0, "updated": 0, "removed": 0, "total": 0}
        corpus = {rel: doc for rel, doc in self._iter_corpus()}
        with closing(self._connect()) as conn:
            # 分词/语料口径不一致 → 先整库重建（迁移自 txxy_test 修订 14 同机理，防混存）
            if self._meta_get(conn, "segmenter_id") != _segmenter_id():
                return self._rebuild_in(conn, corpus, reason="segmenter_mismatch")
            known = {
                row[0]: (row[1], row[2], row[3])
                for row in conn.execute("SELECT rel, kind, mtime_ns, size FROM docs")
            }
            for rel in known:
                if rel not in corpus:
                    self._delete_doc(conn, rel)
                    summary["removed"] += 1
            for rel, doc in corpus.items():
                path = self.guard.kb_root.joinpath(*rel.split("/"))
                try:
                    stat = path.stat()
                except OSError:
                    continue
                old = known.get(rel)
                # kind 也入比对：同 rel 换了 kind（理论不该发生）按变化处理
                if old and old[0] == doc["kind"] and old[1] == stat.st_mtime_ns and old[2] == stat.st_size:
                    continue
                self._insert_doc(conn, rel, stat, doc)
                summary["updated" if old else "added"] += 1
            summary["total"] = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            conn.commit()  # 增量 diff 事务提交（closing 只关连接不提交，勿依赖隐式提交）
        self._last_sync = time.monotonic()
        return summary

    def _rebuild_in(self, conn: sqlite3.Connection, corpus: dict[str, dict], reason: str) -> dict:
        """在给定连接内整库重建（删表重扫，单事务原子提交）。"""
        conn.executescript("DROP TABLE IF EXISTS fts; DROP TABLE IF EXISTS docs;")
        conn.executescript(_SCHEMA)
        self._meta_set(conn, "segmenter_id", _segmenter_id())
        for rel, doc in corpus.items():
            path = self.guard.kb_root.joinpath(*rel.split("/"))
            try:
                stat = path.stat()
            except OSError:
                continue
            self._insert_doc(conn, rel, stat, doc)
        conn.commit()
        self._last_sync = time.monotonic()
        return {"rebuilt": True, "reason": reason, "added": len(corpus), "total": len(corpus)}

    def rebuild(self) -> dict:
        """全量重建指令（§5.3：索引可随时删除重建；手工删除 index.db 后调用同样成立）。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        corpus = {rel: doc for rel, doc in self._iter_corpus()}
        with closing(self._connect()) as conn:
            result = self._rebuild_in(conn, corpus, reason="manual")
            result.pop("added", None)
            result["docs"] = result.pop("total")
        return result

    def ensure_fresh(self, ttl: float = SYNC_TTL) -> None:
        """查询前懒同步：TTL 限频，窗口内不重复扫盘。"""
        if self._last_sync and (time.monotonic() - self._last_sync) < ttl:
            return
        try:
            self.sync()
        except sqlite3.OperationalError:
            # 库文件被占用/损坏不阻塞检索旧数据；重建指令可修复
            pass

    # ---------- 检索 ----------

    def search(self, q: str, limit: int = 20) -> dict:
        tokens = segment(q)
        if not tokens:
            return {"query": q, "results": [], "total": 0}
        self.ensure_fresh()
        match = build_match_query(tokens)
        rows = []
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "SELECT rel, kind, entry_id, title, url, date, tags, status, text_raw, bm25(fts) AS rank"
                " FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                (match, int(limit)),
            )
            for rel, kind, entry_id, title, url, date, tags, status, text_raw, rank in cursor:
                rows.append(
                    {
                        "rel": rel,
                        "kind": kind,
                        "entry_id": entry_id,
                        "title": title,
                        "url": url,
                        "date": date,
                        "tags": tags.split() if tags else [],
                        "status": status,
                        "snippet": self._snippet(text_raw, tokens),
                        "score": round(float(rank), 4),
                    }
                )
        return {"query": q, "results": rows, "total": len(rows)}

    @staticmethod
    def _snippet(text_raw: str, tokens: list[str], window: int = 60) -> str:
        """手动摘要窗口：text_raw 中首个命中 token 前后各取 window 字符。

        不用 FTS snippet()——匹配发生在 text_seg 列（预分词文本），对 text_raw
        列调 snippet() 定位不到命中点。"""
        lowered = text_raw.lower()
        pos = -1
        for tok in tokens:
            pos = lowered.find(tok.lower())
            if pos != -1:
                break
        if pos == -1:
            return text_raw[: window * 2].strip()
        start = max(0, pos - window)
        end = min(len(text_raw), pos + window)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text_raw) else ""
        return prefix + text_raw[start:end].strip() + suffix
