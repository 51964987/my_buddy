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

import logging
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .frontmatter import split_note
from .guard import Guard
from .llm import LLMError, embed_client_for_task

logger = logging.getLogger(__name__)

# 语料文本入索引库的截断上限（控制 index.db 体积；仅影响摘要定位与索引体积）
MAX_RAW_CHARS = 20000

# 懒同步限频（秒）：窗口内重复检索不重复扫盘
SYNC_TTL = 5.0

# 纯符号 / 空白 token 过滤（segment 产物为空时不拼 MATCH）
_PURE_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)

# 向量增强（§5.1 v0.17）：embedding 输入截断与分批大小
EMBED_MAX_CHARS = 4000
EMBED_BATCH = 16


class SemanticSearchError(Exception):
    """语义检索不可用（未启用 / 配置漂移 / embedding 调用失败），由 API 层转 409。"""

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


def load_vec_module():
    """取 sqlite_vec 模块（可选依赖）；未安装返回 None，由调用方按各自口径处理。

    v0.36：此前 `from sqlite_vec import serialize_float32` 是函数内裸导入，缺装时
    语义检索抛 ImportError 变成无信息 500；现语义检索转 409、向量同步转 error 状态。
    """
    try:
        import sqlite_vec
    except ImportError:
        return None
    return sqlite_vec


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
        # 向量增强状态（§5.1 v0.17）：disabled / pending / ready / mismatch / error
        self.vector_enabled = bool(cfg.data.get("index", {}).get("vector_enabled", False))
        self.vector_state = "ready" if self.vector_enabled else "disabled"
        self.vector_error: str | None = None

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
        self._load_vec(conn)
        return conn

    @staticmethod
    def _load_vec(conn: sqlite3.Connection) -> None:
        """挂载 sqlite-vec 扩展（向量增强）；未安装时静默跳过——FTS 全文检索不受影响。"""
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except (ImportError, AttributeError):
            pass

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
            # 分词/语料口径不一致 → 先整库重建（迁移自 txxy_test 修订 14 同机理，防混存）；
            # 分词口径重建 = 全量换语料，向量表随之作废（清 embed_id 强制下次重嵌）
            if self._meta_get(conn, "segmenter_id") != _segmenter_id():
                result = self._rebuild_in(conn, corpus, reason="segmenter_mismatch")
                self._load_vec(conn)
                conn.execute("DELETE FROM meta WHERE key = 'embed_id'")
                conn.executescript("DROP TABLE IF EXISTS vec; DROP TABLE IF EXISTS vec_docs;")
                conn.commit()
                self._last_sync = time.monotonic()
                self._sync_vectors(corpus)
                return result
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
        self._sync_vectors(corpus)
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
        """全量重建指令（§5.3：索引可随时删除重建；手工删除 index.db 后调用同样成立）。

        向量增强开启时同步重建向量表（清除 embed_id 口径标识，强制全量重嵌）。
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        corpus = {rel: doc for rel, doc in self._iter_corpus()}
        with closing(self._connect()) as conn:
            result = self._rebuild_in(conn, corpus, reason="manual")
            result.pop("added", None)
            result["docs"] = result.pop("total")
            # 向量表随全量重建一并重建：清口径标识后按当前配置重嵌
            self._load_vec(conn)
            conn.execute("DELETE FROM meta WHERE key = 'embed_id'")
            conn.executescript("DROP TABLE IF EXISTS vec; DROP TABLE IF EXISTS vec_docs;")
            conn.commit()
        self._sync_vectors(corpus)
        return result

    # ---------- 向量增强（§5.1 v0.17 实施口径） ----------

    def _sync_vectors(self, corpus: dict[str, dict]) -> None:
        """向量表懒同步：与 FTS 同语料同 diff 口径（rel + mtime_ns + size）。

        任何失败只降级向量状态（error/mismatch），绝不影响 FTS 全文检索；
        embedding 口径（model+dimensions）变化 → mismatch，拒绝语义检索、
        由人工经 POST /api/index/rebuild 重建（不自动重嵌，避免静默外部费用）。
        """
        if not self.vector_enabled:
            self.vector_state = "disabled"
            return
        try:
            self._sync_vectors_inner(corpus)
        except Exception as exc:  # 兜底：向量同步任何异常都不得拖垮 FTS 检索
            self.vector_state = "error"
            self.vector_error = f"{type(exc).__name__}: {exc}"

    def _sync_vectors_inner(self, corpus: dict[str, dict]) -> None:
        vec_module = load_vec_module()
        if vec_module is None:
            # 可选依赖缺装：向量状态显式报出（语义检索据此 409），全文检索不受影响
            self.vector_state = "error"
            self.vector_error = "sqlite-vec not installed (pip install sqlite-vec)"
            return
        serialize_float32 = vec_module.serialize_float32

        ai_cfg = self.cfg.data.get("ai", {})
        try:
            client = embed_client_for_task(ai_cfg)
        except LLMError as exc:
            self.vector_state = "error"
            self.vector_error = str(exc)
            return
        dims = client.dimensions
        if not dims:
            self.vector_state = "error"
            self.vector_error = "ai.tasks.embedding.dimensions is not configured"
            return
        embed_id = f"{client.model}:{dims}"
        with closing(self._connect()) as conn:
            cur = self._meta_get(conn, "embed_id")
            if cur and cur != embed_id:
                # 口径漂移：拒绝语义检索并提示人工重建（保留旧向量，不自动重嵌）
                self.vector_state = "mismatch"
                self.vector_error = f"embedding config changed ({cur} -> {embed_id}); rebuild via POST /api/index/rebuild"
                return
            if not cur:
                # 首次启用：清空可能残留的旧维度表后登记口径
                conn.executescript("DROP TABLE IF EXISTS vec; DROP TABLE IF EXISTS vec_docs;")
                self._meta_set(conn, "embed_id", embed_id)
                conn.commit()
            conn.executescript(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec USING vec0(embedding float[{dims}]);"
                "CREATE TABLE IF NOT EXISTS vec_docs("
                "rel TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, vec_rowid INTEGER);"
            )
            known = {
                row[0]: (row[1], row[2], row[3])
                for row in conn.execute("SELECT rel, mtime_ns, size, vec_rowid FROM vec_docs")
            }
            removed = [rel for rel in known if rel not in corpus]
            for rel in removed:
                conn.execute("DELETE FROM vec WHERE rowid = ?", (known[rel][2],))
                conn.execute("DELETE FROM vec_docs WHERE rel = ?", (rel,))
            pending: list[tuple[str, dict, object]] = []
            for rel, doc in corpus.items():
                path = self.guard.kb_root.joinpath(*rel.split("/"))
                try:
                    stat = path.stat()
                except OSError:
                    continue
                old = known.get(rel)
                if old and old[0] == stat.st_mtime_ns and old[1] == stat.st_size:
                    continue
                pending.append((rel, doc, stat))
            for i in range(0, len(pending), EMBED_BATCH):
                chunk = pending[i : i + EMBED_BATCH]
                texts = [self._embed_text(doc) for _, doc, _ in chunk]
                vecs = client.embed(texts)
                for (rel, _doc, stat), vec in zip(chunk, vecs):
                    old_rowid = known.get(rel, (None, None, None))[2]
                    if old_rowid is not None:
                        conn.execute("DELETE FROM vec WHERE rowid = ?", (old_rowid,))
                    ins = conn.execute(
                        "INSERT INTO vec(embedding) VALUES (?)", (serialize_float32(vec),)
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO vec_docs(rel, mtime_ns, size, vec_rowid)"
                        " VALUES (?, ?, ?, ?)",
                        (rel, stat.st_mtime_ns, stat.st_size, ins.lastrowid),
                    )
            conn.commit()
        self.vector_state = "ready"
        self.vector_error = None

    @staticmethod
    def _embed_text(doc: dict) -> str:
        """embedding 输入：与 FTS 分词输入同源（标题+tags+正文），另做长度截断控成本。"""
        return f"{doc['title']}\n{doc['tags']}\n{doc['text_raw'][:EMBED_MAX_CHARS]}"

    def search_semantic(self, q: str, limit: int = 20) -> dict:
        """语义检索（GET /api/search?mode=semantic）：KNN 最近邻，显式 k（txxy 踩坑口径）。"""
        if not self.vector_enabled:
            raise SemanticSearchError("vector index is disabled (config index.vector_enabled)")
        vec_module = load_vec_module()
        if vec_module is None:
            # 可选依赖缺装 → 与「未启用/口径漂移」同一出口（API 层 409），不裸 500
            raise SemanticSearchError("sqlite-vec not installed (pip install sqlite-vec); full-text search unaffected")
        serialize_float32 = vec_module.serialize_float32
        if self.vector_state == "mismatch":
            raise SemanticSearchError(self.vector_error or "embedding mismatch; rebuild index")

        self.ensure_fresh()  # 懒同步可能更新 vector_state
        if self.vector_state == "mismatch":
            raise SemanticSearchError(self.vector_error or "embedding mismatch; rebuild index")
        if self.vector_state == "error":
            raise SemanticSearchError(f"embedding unavailable: {self.vector_error}")
        ai_cfg = self.cfg.data.get("ai", {})
        try:
            client = embed_client_for_task(ai_cfg)
            qvec = client.embed([q.strip()[:EMBED_MAX_CHARS]])[0]
        except LLMError as exc:
            raise SemanticSearchError(f"query embedding failed: {exc}") from exc
        with closing(self._connect()) as conn:
            # vec0 KNN 返回 rowid + distance（0.1.x 无元数据列）；rel 经 vec_docs 映射
            rows = conn.execute(
                "SELECT rowid, distance FROM vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (serialize_float32(qvec), int(limit)),
            ).fetchall()
            results = []
            for vec_rowid, dist in rows:
                rel_row = conn.execute(
                    "SELECT rel FROM vec_docs WHERE vec_rowid = ?", (vec_rowid,)
                ).fetchone()
                if rel_row is None:
                    continue
                rel = rel_row[0]
                row = conn.execute(
                    "SELECT kind, entry_id, title, url, date, tags, status, text_raw"
                    " FROM fts WHERE rel = ?",
                    (rel,),
                ).fetchone()
                if row is None:
                    continue  # FTS 与向量偶发不同步（如语料正被编辑），跳过该行
                kind, entry_id, title, url, date, tags, status, text_raw = row
                results.append(
                    {
                        "rel": rel,
                        "kind": kind,
                        "entry_id": entry_id,
                        "title": title,
                        "url": url,
                        "date": date,
                        "tags": tags.split() if tags else [],
                        "status": status,
                        "snippet": self._snippet(text_raw or "", []),
                        "distance": round(float(dist), 4),
                    }
                )
        return {"query": q, "mode": "semantic", "results": results, "total": len(results)}

    def index_status(self) -> dict:
        """索引概览（运维/总览页）：不触发同步，只读计数。"""
        docs = 0
        embedded = 0
        if self.db_path.exists():
            try:
                with closing(self._connect()) as conn:
                    docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
                    try:
                        embedded = conn.execute("SELECT COUNT(*) FROM vec_docs").fetchone()[0]
                    except sqlite3.OperationalError:
                        embedded = 0  # 向量表未建（未启用/未同步）
            except sqlite3.Error as exc:  # 含 DatabaseError（库损坏/磁盘错误），静默会误报"0 篇"
                logger.warning("读取索引状态失败（docs 显示为 0，POST /api/index/rebuild 可修复）：%s: %s", type(exc).__name__, exc)
        return {
            "docs": docs,
            "vector": {
                "enabled": self.vector_enabled,
                "state": self.vector_state,
                "embedded": embedded,
                "error": self.vector_error,
            },
        }

    def ensure_fresh(self, ttl: float = SYNC_TTL) -> None:
        """查询前懒同步：TTL 限频，窗口内不重复扫盘。

        索引库异常（占用/损坏/磁盘错误）不阻塞检索旧数据，但必须留日志——
        v0.36：此前只捕 `sqlite3.OperationalError`，漏了父类 `DatabaseError`
        （如 malformed 库），异常穿透成无信息 500。
        """
        if self._last_sync and (time.monotonic() - self._last_sync) < ttl:
            return
        try:
            self.sync()
        except sqlite3.Error as exc:
            logger.warning("索引懒同步失败（本次检索使用现有索引，POST /api/index/rebuild 可修复）：%s: %s", type(exc).__name__, exc)

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
