"""FTS5 全文索引引擎（P4，v0.15 实施口径）回归用例。

覆盖：中文检索命中、增量同步（增/改/删）、error 条目不入索引、
查询语法安全（特殊字符/保留词/空查询）、口径变更自动整库重建、可牺牲性（删库后可重建）。
"""

import time

import yaml

from kbserver.indexer import Indexer, build_match_query, segment


def make_note(guard, rel: str, *, title: str, body: str, status: str = "normalized", entry_id: str = "e1", url: str = "https://example.com/x", tags=None):
    """按 normalize 产物形态写一条源条目（frontmatter + 正文），经守卫落盘。"""
    fm = {
        "id": entry_id,
        "title": title,
        "url": url,
        "platform": "web",
        "source_type": "webpage",
        "status": status,
        "captured_at": "2026-10-02T10:00:00+08:00",
    }
    if tags is not None:
        fm["tags"] = tags
    text = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + body
    guard.write_text("normalize", rel, text)


def make_wiki_card(guard, rel: str, *, card_id: str, title: str, body: str):
    fm = {"id": card_id, "type": "summary", "ai_generated": True, "status": "draft", "created_at": "2026-10-02T10:00:00+08:00"}
    text = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + f"# {title}\n\n{body}"
    guard.write_text("enrich", rel, text)


def touch(path):
    """mtime 粒度依赖文件系统（Windows 约 100ns~15ms），写后强制推进 mtime。"""
    st = path.stat()
    import os

    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_segment_and_match_query_safety():
    # 搜索引擎模式：长词切出子词
    toks = segment("摩托车保养手册")
    assert "摩托车" in toks
    # 特殊字符 / 保留词被过滤或安全包裹
    assert build_match_query(segment('好"的 (OR) NOT')).count('"') % 2 == 0
    # 纯符号查询 → 空 token（调用方须判空，不拼 MATCH）
    assert segment('!@# $%^ (*")') == []


def test_chinese_search_hits_entry_and_wiki(cfg, guard):
    make_note(guard, "sources/a/note.md", title="SQLite 全文检索笔记", body="FTS5 建表要显式配置中文分词，否则中文召回无效。", tags=["数据库"])
    make_wiki_card(guard, "wiki/w-abc123.md", card_id="w-abc123", title="摘要卡：分词", body="要点：unicode61 对中文无效。")
    idx = Indexer(cfg, guard)
    result = idx.search("中文分词")
    assert result["total"] == 2
    kinds = {r["kind"] for r in result["results"]}
    assert kinds == {"entry", "wiki"}
    entry = next(r for r in result["results"] if r["kind"] == "entry")
    assert entry["entry_id"] == "e1"
    assert entry["url"] == "https://example.com/x"
    assert entry["tags"] == ["数据库"]
    # 1~2 字短查询（trigram 方案的召回硬伤场景）也能命中
    assert idx.search("分词")["total"] >= 1


def test_error_entries_excluded(cfg, guard):
    make_note(guard, "sources/a/note.md", title="正常条目", body="内容甲。", status="normalized")
    make_note(guard, "sources/b/note.md", title="错误条目", body="机密错误内容乙。", status="error", entry_id="e2")
    idx = Indexer(cfg, guard)
    idx.rebuild()
    assert idx.search("错误内容")["total"] == 0
    assert idx.search("内容甲")["total"] == 1


def test_incremental_sync_add_update_remove(cfg, guard):
    make_note(guard, "sources/a/note.md", title="条目甲", body="初始内容。")
    idx = Indexer(cfg, guard)
    first = idx.sync()
    assert first["added"] == 1 and first["total"] == 1

    # 无变化 → 零增量
    again = idx.sync()
    assert again["added"] == 0 and again["updated"] == 0 and again["removed"] == 0

    # 内容变化 → updated，新词可检索、旧词不可检索
    make_note(guard, "sources/a/note.md", title="条目甲", body="更新后的向量检索内容。")
    touch(guard.kb_root / "sources/a/note.md")
    updated = idx.sync()
    assert updated["updated"] == 1
    assert idx.search("向量检索")["total"] == 1
    assert idx.search("初始内容")["total"] == 0

    # 文件消失 → removed，索引随事实源
    (guard.kb_root / "sources/a/note.md").unlink()
    removed = idx.sync()
    assert removed["removed"] == 1
    assert idx.search("向量检索")["total"] == 0


def test_search_lazy_sync_fresh_results(cfg, guard):
    """检索 API 口径：查询前懒同步，落盘变化即时反映到检索结果。"""
    make_note(guard, "sources/a/note.md", title="条目甲", body="懒同步前内容。")
    idx = Indexer(cfg, guard)
    assert idx.search("懒同步")["total"] == 1
    make_note(guard, "sources/b/note.md", title="条目乙", body="懒同步后新增的内容。")
    touch(guard.kb_root / "sources/b/note.md")
    # TTL 内仍会扫盘吗？——TTL 只跳过上次同步后的窗口，这里刚同步过，强制过期
    idx._last_sync = 0.0
    assert idx.search("新增")["total"] == 1


def test_segmenter_mismatch_triggers_full_rebuild(cfg, guard):
    make_note(guard, "sources/a/note.md", title="条目甲", body="内容。")
    idx = Indexer(cfg, guard)
    idx.rebuild()
    # 模拟换分词器/语料口径（如 jieba 版本升级）：meta 口径标识不一致 → 自动整库重建
    import sqlite3

    with sqlite3.connect(idx.db_path) as conn:
        conn.execute("UPDATE meta SET value = 'stale' WHERE key = 'segmenter_id'")
    summary = idx.sync()
    assert summary.get("rebuilt") is True
    assert idx.search("内容")["total"] == 1


def test_index_is_sacrificable(cfg, guard):
    """索引可牺牲：删掉 index.db 后首次检索自动重建，数据零丢失。"""
    make_note(guard, "sources/a/note.md", title="条目甲", body="删除索引后仍可检索。")
    idx = Indexer(cfg, guard)
    idx.rebuild()
    idx.db_path.unlink()
    idx._last_sync = 0.0
    assert idx.search("删除索引")["total"] == 1


def test_query_special_chars_no_crash(cfg, guard):
    make_note(guard, "sources/a/note.md", title="条目甲", body="内容包含 AND OR NOT 等词。")
    idx = Indexer(cfg, guard)
    idx.rebuild()
    for q in ('"', "((", "OR", "NEAR(a,b)", "a - b", "^x", "中文 OR"):
        assert idx.search(q)["total"] >= 0  # 不抛 OperationalError 即通过


def test_ensure_fresh_ttl_skips_rescan(cfg, guard):
    make_note(guard, "sources/a/note.md", title="条目甲", body="内容。")
    idx = Indexer(cfg, guard)
    idx.rebuild()
    make_note(guard, "sources/b/note.md", title="条目乙", body="新内容。")
    # TTL 窗口内：不扫盘，新文件不进索引
    idx.ensure_fresh()
    assert idx.search("新内容")["total"] == 0
    # TTL 过期：下一次检索可见
    idx._last_sync = time.monotonic() - 10
    assert idx.search("新内容")["total"] == 1
