import pytest

from kbserver.config import Config
from kbserver.guard import Guard
from kbserver.normalize import FetchResult

SAMPLE_HTML = """<html><head><title>sample page</title></head><body>
<article><h1>sample page</h1>
<p>first paragraph for extraction testing.</p>
<p>second paragraph.</p>
</article></body></html>"""


def make_fetcher(pages=None, images=None, fail_urls=None):
    pages = pages if pages is not None else {}
    images = images if images is not None else {}
    fail_urls = fail_urls if fail_urls is not None else set()

    def fetch(url: str) -> FetchResult:
        if url in fail_urls:
            raise RuntimeError(f"boom: {url}")
        if url in pages:
            return FetchResult(
                url=url,
                final_url=url,
                content=pages[url].encode("utf-8"),
                encoding="utf-8",
                status_code=200,
                content_type="text/html; charset=utf-8",
            )
        if url in images:
            return FetchResult(
                url=url,
                final_url=url,
                content=images[url],
                encoding="utf-8",
                status_code=200,
                content_type="image/png",
            )
        raise RuntimeError(f"no fixture for {url}")

    return fetch


@pytest.fixture
def cfg(tmp_path):
    c = Config(path=tmp_path / "kbserver.config.json")
    c.data["kb_root"] = str(tmp_path / "kb")
    c.data["pipeline"]["worker_enabled"] = False
    # 测试默认关闭 Playwright 兜底：假 fetcher 失败后真浏览器会抓到真实页面，破坏确定性；
    # 兜底行为由注入 playwright_fetcher 的专项用例覆盖
    c.data["normalize"]["playwright_fallback"] = False
    c.data["index"]["vector_enabled"] = False
    return c


@pytest.fixture
def guard(cfg):
    return Guard(cfg.kb_root)
