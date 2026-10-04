"""scripts/migrate_image_map.py 存量图片映射回填测试（§6 v0.32）。

场景：v0.32 之前 meta.json raw_files 图片条目为 str（无原站 URL 映射）；
脚本解析 raw/page.json（站点 API 原件，内含原始 MDContent）重抓对齐 sha1 回填。
"""

import importlib.util
import json
from pathlib import Path

from .test_export import _image_name, _synced_with_image

# 按路径加载脚本模块（scripts/ 非包）
_SPEC = importlib.util.spec_from_file_location(
    "migrate_image_map", Path(__file__).resolve().parents[1] / "scripts" / "migrate_image_map.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

IMAGE_URL = "https://img.example.com/a.png"
IMAGE_BYTES = b"\x89PNG-fake-bytes"
META_REL = Path("collections/test-lib/docs/Guide/Intro/meta.json")


class _FakeResp:
    def __init__(self):
        self.content = IMAGE_BYTES
        self.headers = {"content-type": "image/png"}

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        return _FakeResp()


def _rollback_to_legacy_shape(kb_root: Path) -> str:
    """把已同步条目回退为 v0.32 之前形状（str 条目 + raw/page.json 含原 MDContent），返回图片名。"""
    meta_p = kb_root / META_REL
    name = _image_name(IMAGE_BYTES)
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["raw_files"] = [f"raw/{name}", "raw/page.json"]
    meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    # raw/page.json 原件在真实库中保留原始 MDContent（含原站图片 URL）；测试写入等价 fixture
    page_json = meta_p.parent / "raw" / "page.json"
    page_json.write_bytes(json.dumps({"Result": {"MDContent": f"![p]({IMAGE_URL})"}}).encode("utf-8"))
    return name


def test_migrate_entry_backfills_mapping(cfg, guard, monkeypatch):
    _synced_with_image(cfg, guard)
    name = _rollback_to_legacy_shape(cfg.kb_root)
    monkeypatch.setattr(mod.httpx, "Client", _FakeClient)

    mapped, unresolved = mod.migrate_entry(cfg.kb_root, META_REL, apply=True)

    assert (mapped, unresolved) == (1, 0)
    meta = json.loads((cfg.kb_root / META_REL).read_text(encoding="utf-8"))
    entry = next(e for e in meta["raw_files"] if isinstance(e, dict))
    assert entry == {"path": f"raw/{name}", "src": IMAGE_URL}  # sha1 对齐 + 原站 URL
    assert "raw/page.json" in meta["raw_files"]  # 非图片原件维持 str


def test_migrate_entry_dry_run_no_write(cfg, guard, monkeypatch):
    _synced_with_image(cfg, guard)
    name = _rollback_to_legacy_shape(cfg.kb_root)
    monkeypatch.setattr(mod.httpx, "Client", _FakeClient)

    mapped, unresolved = mod.migrate_entry(cfg.kb_root, META_REL, apply=False)

    assert (mapped, unresolved) == (1, 0)  # 报告口径一致
    meta = json.loads((cfg.kb_root / META_REL).read_text(encoding="utf-8"))
    assert meta["raw_files"] == [f"raw/{name}", "raw/page.json"]  # dry-run 未写盘


def test_migrate_entry_no_page_json_counts_unresolved(cfg, guard):
    _synced_with_image(cfg, guard)
    meta_p = cfg.kb_root / META_REL
    name = _image_name(IMAGE_BYTES)
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["raw_files"] = [f"raw/{name}"]  # 无 raw/page.json 原件（如 A/B/C 类或原件缺失）
    meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    mapped, unresolved = mod.migrate_entry(cfg.kb_root, META_REL, apply=True)

    assert (mapped, unresolved) == (0, 1)  # 保持无映射，导出侧走 API 回落
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    assert meta["raw_files"] == [f"raw/{name}"]  # 不改写
