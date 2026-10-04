"""全文导出测试（§6 v0.29/v0.32）：merged 单文件 / zip 中文标题布局 / pages 载荷 / error 占位 / 三态。"""

import io
import json
import zipfile

from fastapi.testclient import TestClient

from kbserver.app import create_app
from kbserver.orchestrator import Orchestrator
from kbserver.sync import SiteNode, SitePage, SyncEngine

from .conftest import make_fetcher
from .test_sync import FakeParser, base_nodes, base_pages, make_payload, register_payload


def make_engine(cfg, guard, nodes=None, pages=None):
    parser = FakeParser(nodes if nodes is not None else base_nodes(), pages if pages is not None else base_pages())
    return SyncEngine(cfg, guard, parsers={parser.name: parser}), parser


def make_client(cfg, guard, engine):
    orch = Orchestrator(cfg, guard, fetcher=make_fetcher())
    orch.sync_engine = engine
    app = create_app(cfg, orchestrator=orch)
    return TestClient(app)


def setup_synced(cfg, guard, nodes=None, pages=None):
    engine, parser = make_engine(cfg, guard, nodes, pages)
    engine.register_collection(register_payload())
    engine.sync("test-lib")
    return make_client(cfg, guard, engine), parser


# ---------- 单元：大纲树与标题降级 / v0.32 命名 ----------


def test_clean_segment_windows_rules():
    # v0.32：Windows 非法字符替换、尾点空格剥除、保留名加前缀、空标题回退
    from kbserver.exporter import _clean_segment

    assert _clean_segment('a/b:c*?"<>|', "x") == "a b c"  # 非法字符 → 空格并剥尾
    assert _clean_segment("名称.", "x") == "名称"  # 尾点剥除
    assert _clean_segment("CON", "x") == "_CON"  # Windows 保留名
    assert _clean_segment("  ", "回退") == "回退"  # 空标题回退
    assert len(_clean_segment("长" * 300, "x")) == 100  # 段长上限


def test_assign_names_dedup_and_toc_reserve():
    # v0.32：同层同名 -2 判重；根层页面预留 toc（索引文件占用）
    from kbserver.exporter import _assign_names

    outline = [
        {
            "kind": "dir",
            "path": "D",
            "title": "目录",
            "index": 0,
            "children": [
                {"kind": "page", "path": "D/a", "title": "同名", "url": "", "index": 0, "children": []},
                {"kind": "page", "path": "D/b", "title": "同名", "url": "", "index": 0, "children": []},
            ],
        },
        {"kind": "page", "path": "root", "title": "TOC", "url": "", "index": 0, "children": []},
    ]
    dirs, stems = _assign_names(outline)
    assert dirs["D"] == "目录"
    assert stems["D/a"] == "目录/同名"
    assert stems["D/b"] == "目录/同名-2"
    assert stems["root"] == "TOC-2"  # casefold 判重，toc 为索引文件保留


# ---------- 单元：大纲树与标题降级 ----------


def test_build_outline_error_pages_sorted_last(cfg, guard):
    # error 页不在 toc.json（来自 sync.errors），排在同层末尾；标题取 error 记录（v0.29 补字段）
    from kbserver.exporter import build_outline

    toc = {
        "pages": [
            {"path": "Guide/Intro", "title": "简介", "url": "u1", "index": 2},
            {"path": "Guide/Setup", "title": "安装", "url": "u2", "index": 1},
        ],
        "dirs": [{"path": "Guide", "title": "指南", "index": 0}],
    }
    errors = [{"path": "Guide/Broken", "title": "坏页", "index": 0, "error_message": "boom"}]
    outline = build_outline(toc, errors)
    guide = outline[0]
    assert [c["title"] for c in guide["children"]] == ["安装", "简介", "坏页"]
    assert guide["children"][2]["kind"] == "error"


def test_shift_headings_skips_fenced_code():
    from kbserver.exporter import _shift_headings

    body = "# t\n\n```python\n# comment not heading\n```\n\n## sub"
    out = _shift_headings(body, 2)
    assert "### t" in out
    assert "# comment not heading" in out  # 围栏内原样
    assert "#### sub" in out


# ---------- API：merged ----------


def test_export_merged_inline_body_and_headings(cfg, guard):
    # 正文内联：大纲标题 + 正文 + 原文链接；正文标题按大纲深度降级
    pages = {
        2: SitePage(url="https://x/2", title="简介", markdown="intro line\n\n## inner head", raw=b"{}"),
        3: SitePage(url="https://x/3", title="简介二", markdown="body two", raw=b"{}"),
    }
    client, _ = setup_synced(cfg, guard, pages=pages)
    resp = client.get("/api/collections/test-lib/export?format=merged")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    md = resp.text
    assert "# 测试文档站（全文导出）" in md
    assert "## 指南" in md  # 顶级目录 → ##（depth=1 → level=2）
    assert "### 简介" in md  # 顶级目录下页面 → ###
    assert "intro line" in md
    assert "[原文](https://x/2)" in md
    assert "##### inner head" in md  # 正文 ## 降级（2+3=5）
    assert "body two" in md


def test_export_merged_error_page_placeholder(cfg, guard):
    # error 页（抓取失败）占位标注"该页抓取失败"（用户拍板 3）
    pages = {2: make_payload("intro content", 2, "Intro")}  # doc 3 缺失 → error
    client, _ = setup_synced(cfg, guard, pages=pages)
    md = client.get("/api/collections/test-lib/export?format=merged").text
    assert "该页抓取失败" in md
    assert "page-Intro" in md  # v0.29：error 记录带 title（取节点/页面标题）


# ---------- API：zip ----------


def test_export_zip_layout(cfg, guard):
    # v0.32：中文标题布局——目录标题=文件夹、页面标题=文件名；去 frontmatter 纯原文 + 灰色原文 URL 行
    client, _ = setup_synced(cfg, guard)
    resp = client.get("/api/collections/test-lib/export?format=zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "toc.md" in names
    # 两页同 code（路径冲突编号化）同标题 → 同层重名 -2
    assert "指南/page-Intro.md" in names
    assert "指南/page-Intro-2.md" in names
    toc_md = zf.read("toc.md").decode("utf-8")
    assert "[page-Intro](指南/page-Intro.md)" in toc_md
    note = zf.read("指南/page-Intro.md").decode("utf-8")
    assert note.startswith("# page-Intro\n")  # 无 frontmatter，标题起头
    assert "intro content v1" in note  # 纯原文
    assert '<span style="color:#888888">原文：https://docs.example.com/docs/TestLib/doc/Intro</span>' in note
    assert "captured_at" not in note  # frontmatter 元数据已剥


def test_export_zip_error_page_placeholder(cfg, guard):
    # v0.32：error 页落占位 md（标题=error 记录标题），目录树完整性不缺页
    pages = {2: make_payload("intro content", 2, "Intro")}  # doc 3 缺失 → error
    client, _ = setup_synced(cfg, guard, pages=pages)
    zf = zipfile.ZipFile(io.BytesIO(client.get("/api/collections/test-lib/export?format=zip").content))
    placeholders = [n for n in zf.namelist() if n != "toc.md" and not n.endswith("page-Intro.md")]
    assert len(placeholders) == 1
    assert placeholders[0].startswith("指南/")  # 与正常页同目录
    content = zf.read(placeholders[0]).decode("utf-8")
    assert content.startswith("# ")
    assert "该页抓取失败" in content


def test_export_zip_includes_images(cfg, guard):
    # v0.32：图片集中到所在目录 raw/（跨页同名即同内容）；normalize 在 meta.json 记录原站映射
    nodes = [
        SiteNode(doc_id=1, code="Guide", parent_id=0, is_dir=True, title="指南"),
        SiteNode(doc_id=2, code="Intro", parent_id=1, is_dir=False, title="简介"),
    ]
    pages_map = {
        2: SitePage(url="https://x/2", title="简介", markdown="![pic](https://img.example.com/a.png)", raw=b"{}"),
    }
    engine = SyncEngine(cfg, guard, parsers={"fake": FakeParser(nodes, pages_map)})
    engine._make_fetcher = lambda: make_fetcher(images={"https://img.example.com/a.png": b"png-bytes"})
    engine.register_collection(register_payload())
    engine.sync("test-lib")
    client = make_client(cfg, guard, engine)
    zf = zipfile.ZipFile(io.BytesIO(client.get("/api/collections/test-lib/export?format=zip").content))
    imgs = [n for n in zf.namelist() if n.startswith("指南/raw/img-")]
    assert len(imgs) == 1
    assert zf.read(imgs[0]) == b"png-bytes"
    # v0.32：meta.json raw_files 图片条目为 {path, src}（原站 URL 映射）
    meta = json.loads(
        (cfg.kb_root / "collections/test-lib/docs/Guide/Intro/meta.json").read_text(encoding="utf-8")
    )
    img_entry = next(e for e in meta["raw_files"] if isinstance(e, dict))
    assert img_entry["path"] == f"raw/{_image_name(b'png-bytes')}"
    assert img_entry["src"] == "https://img.example.com/a.png"
    # 非图片原件维持 str 形状
    assert "raw/page.json" in meta["raw_files"]


# ---------- API：三态与参数 ----------


def test_export_tri_state_and_params(cfg, guard):
    engine, _ = make_engine(cfg, guard)
    client = make_client(cfg, guard, engine)
    # 404：collection 不存在
    assert client.get("/api/collections/missing/export").status_code == 404
    # 409：已注册但首抓未完成（register_collection 只落 collection.json，不抓取）
    engine.register_collection(register_payload())
    assert client.get("/api/collections/test-lib/export?format=zip").status_code == 409
    assert client.get("/api/collections/test-lib/export?format=pages").status_code == 409
    # 422：未知格式（需先完成首抓，否则先命中 409）
    engine.sync("test-lib")
    client = make_client(cfg, guard, engine)
    assert client.get("/api/collections/test-lib/export?format=pdf").status_code == 422
    # v0.32：pages 分支
    assert client.get("/api/collections/test-lib/export?format=pages").status_code == 200


def test_export_pages_payload(cfg, guard):
    # v0.32：format=pages——zip_path 后端统一计算（单一命名事实源）、toc_md 链接 .docx
    client, _ = setup_synced(cfg, guard)
    resp = client.get("/api/collections/test-lib/export?format=pages")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "测试文档站"
    by_path = {p["path"]: p for p in data["pages"]}
    assert by_path["Guide/Intro"]["zip_path"] == "指南/page-Intro"
    assert by_path["Guide/Intro"]["kind"] == "page"
    assert by_path["Guide/Intro"]["body"] == "intro content v1"
    assert by_path["Guide/Intro"]["url"].startswith("https://docs.example.com/")
    assert "[page-Intro](指南/page-Intro.docx)" in data["toc_md"]


# ---------- v0.31：image 端点与 merged images=api ----------


def _synced_with_image(cfg, guard):
    """同步一个含图片页面的 collection（本地化产物 raw/img-<sha1>.png）。"""
    nodes = [
        SiteNode(doc_id=1, code="Guide", parent_id=0, is_dir=True, title="指南"),
        SiteNode(doc_id=2, code="Intro", parent_id=1, is_dir=False, title="简介"),
    ]
    pages_map = {
        2: SitePage(url="https://x/2", title="简介", markdown="![pic](https://img.example.com/a.png)", raw=b"{}"),
    }
    engine = SyncEngine(cfg, guard, parsers={"fake": FakeParser(nodes, pages_map)})
    engine._make_fetcher = lambda: make_fetcher(images={"https://img.example.com/a.png": b"\x89PNG-fake-bytes"})
    engine.register_collection(register_payload())
    engine.sync("test-lib")
    return make_client(cfg, guard, engine)


def _image_name(content: bytes) -> str:
    import hashlib

    return f"img-{hashlib.sha1(content).hexdigest()[:10]}.png"


def test_image_endpoint_serves_localized_image(cfg, guard):
    # image 端点放行 docs/<path>/raw/img-*：200 + 字节一致 + media type
    content = b"\x89PNG-fake-bytes"
    client = _synced_with_image(cfg, guard)
    name = _image_name(content)
    from urllib.parse import quote

    url = f"/api/collections/test-lib/image?path={quote('Guide/Intro', safe='')}&name={quote(name, safe='')}"
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.content == content
    assert resp.headers["content-type"] == "image/png"


def test_image_endpoint_rejects_traversal_and_non_image(cfg, guard):
    # 防穿越与非 img-* 文件名拒绝：422（name 校验）/ 404（resolve 后越界或不存在）
    from urllib.parse import quote

    client = _synced_with_image(cfg, guard)
    p = quote("Guide/Intro", safe="")
    # name 非 img- 前缀
    assert client.get(f"/api/collections/test-lib/image?path={p}&name=note.md").status_code == 422
    # name 含路径穿越
    assert client.get(f"/api/collections/test-lib/image?path={p}&name=img-..%2F..%2Fx.png").status_code == 422
    # path 穿越 resolve 后越界 → 404
    bad_path = quote("../../../../etc", safe="")
    assert client.get(f"/api/collections/test-lib/image?path={bad_path}&name=img-abc.png").status_code == 404
    # 不存在的图片
    assert client.get(f"/api/collections/test-lib/image?path={p}&name=img-0000000000.png").status_code == 404


def test_export_merged_images_modes(cfg, guard):
    # v0.32 三模式：original（默认）映射→原站 URL；api 全走 image 端点；relative 保留相对引用
    content = b"\x89PNG-fake-bytes"
    client = _synced_with_image(cfg, guard)
    name = _image_name(content)
    orig_md = client.get("/api/collections/test-lib/export?format=merged").text
    assert "](https://img.example.com/a.png)" in orig_md  # 映射命中 → 原站地址
    assert "](raw/img-" not in orig_md
    api_md = client.get("/api/collections/test-lib/export?format=merged&images=api").text
    assert f"/api/collections/test-lib/image?path=Guide%2FIntro&name={name}" in api_md
    assert "](raw/img-" not in api_md
    rel_md = client.get("/api/collections/test-lib/export?format=merged&images=relative").text
    assert f"](raw/{name})" in rel_md


def test_export_merged_original_falls_back_to_api_url(cfg, guard):
    # v0.32：无映射（存量 str 形状 raw_files）→ 回落 image API URL，图片仍可达
    content = b"\x89PNG-fake-bytes"
    client = _synced_with_image(cfg, guard)
    name = _image_name(content)
    meta_p = cfg.kb_root / "collections/test-lib/docs/Guide/Intro/meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["raw_files"] = [f"raw/{name}", "raw/page.json"]  # 模拟 v0.32 之前的形状
    meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    md = client.get("/api/collections/test-lib/export?format=merged").text
    assert f"/api/collections/test-lib/image?path=Guide%2FIntro&name={name}" in md
    assert "](raw/img-" not in md


def test_export_merged_images_param_validation(cfg, guard):
    # images 参数白名单：非法值 422
    client = _synced_with_image(cfg, guard)
    assert client.get("/api/collections/test-lib/export?format=merged&images=bogus").status_code == 422


def test_rewrite_image_links_skips_fenced_code():
    # 围栏代码块内的 raw/img-* 引用原样保留（与 _shift_headings 同一围栏口径）
    from kbserver.exporter import _rewrite_image_links

    body = "![pic](raw/img-a.png)\n\n```markdown\n![x](raw/img-b.png)\n```\n\n![pic2](raw/img-c.png \"title\")"
    out = _rewrite_image_links(body, lambda rel: f"/api/x?name={rel}")
    assert "![pic](/api/x?name=raw/img-a.png)" in out
    assert "![x](raw/img-b.png)" in out  # 围栏内原样
    assert '![pic2](/api/x?name=raw/img-c.png "title")' in out  # 带标题语法保留
