"""事件总线与 SSE 端点（v0.53 §11.8 ⑥）回归用例。

口径对应：
- 无 loop / 无订阅者时 publish no-op（零成本，不影响写盘主流程）；
- 同线程与工作线程两条发布路径（call_soon_threadsafe）；
- 队列满丢最旧（幂等触发语义）；
- 守卫成功写盘发 kb.changed、失败（越界）不发；
- SyncEngine._set_progress 内存态与事件同源；
- GET /api/events 流：hello 帧 + 事件投递 + 鉴权。
"""

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from kbserver import events
from kbserver.app import create_app
from kbserver.guard import Guard, WriteBoundaryError
from kbserver.sync import SyncEngine


def _run_with_bus(body):
    """在临时事件循环里执行 body（注入 loop，结束清理，避免污染其他用例）。"""

    async def runner():
        loop = asyncio.get_running_loop()
        events.set_loop(loop)
        try:
            return await body(loop)
        finally:
            events.set_loop(None)

    return asyncio.run(runner())


def test_publish_without_loop_is_noop():
    events.set_loop(None)
    events.publish("kb.changed", stage="normalize")  # 不得抛出
    assert events.subscriber_count() == 0


def test_publish_without_subscribers_is_noop():
    async def body(_loop):
        events.publish("kb.changed", stage="normalize")  # 有 loop 无订阅者

    _run_with_bus(body)


def test_pubsub_roundtrip_same_thread():
    async def body(_loop):
        q = await events.subscribe()
        events.publish("kb.changed", stage="normalize", op="write")
        payload = await asyncio.wait_for(q.get(), timeout=2.0)
        assert payload["type"] == "kb.changed"
        assert payload["stage"] == "normalize"
        assert payload["op"] == "write"
        events.unsubscribe(q)
        assert events.subscriber_count() == 0

    _run_with_bus(lambda loop: body(loop))


def test_publish_from_worker_thread():
    """守卫/sync 的写盘多发生在工作线程：验证 call_soon_threadsafe 跨线程路径。"""

    async def body(_loop):
        q = await events.subscribe()

        def worker():
            events.publish("sync.progress", collection_id="c1", phase="fetch", done=1, total=2, errors=0)

        t = threading.Thread(target=worker)
        t.start()
        payload = await asyncio.wait_for(q.get(), timeout=2.0)
        t.join()
        assert payload["type"] == "sync.progress"
        assert payload["done"] == 1
        events.unsubscribe(q)

    _run_with_bus(body)


def test_queue_overflow_drops_oldest():
    async def body(_loop):
        q = await events.subscribe()
        for i in range(events.QUEUE_SIZE + 10):
            events.publish("kb.changed", n=i)
        await asyncio.sleep(0.1)  # 等投递回调执行完
        seq = []
        while not q.empty():
            seq.append(q.get_nowait()["n"])
        assert len(seq) == events.QUEUE_SIZE
        assert seq[-1] == events.QUEUE_SIZE + 9  # 最新事件保留
        events.unsubscribe(q)

    _run_with_bus(body)


def test_guard_write_publishes_kb_changed(tmp_path):
    async def body(_loop):
        q = await events.subscribe()
        g = Guard(tmp_path)
        g.write_text("normalize", "sources/x/note.md", "# t")
        payload = await asyncio.wait_for(q.get(), timeout=2.0)
        assert payload["type"] == "kb.changed"
        assert payload["stage"] == "normalize"
        assert payload["op"] == "write"
        events.unsubscribe(q)

    _run_with_bus(body)


def test_guard_rejected_write_publishes_nothing(tmp_path):
    """越界写被拒绝（铁律守卫职责）不得发事件——只有成功落盘才通知。"""

    async def body(_loop):
        q = await events.subscribe()
        g = Guard(tmp_path)
        # index 阶段只允许 index.db（STAGE_REGIONS），写 sources/ 必被拒
        with pytest.raises(WriteBoundaryError):
            g.write_text("index", "sources/x/note.md", "# t")
        assert q.empty()
        events.unsubscribe(q)

    _run_with_bus(body)


def test_sync_set_progress_memory_and_event_aligned(cfg, tmp_path):
    async def body(_loop):
        q = await events.subscribe()
        engine = SyncEngine(cfg, Guard(tmp_path))
        engine._set_progress("c1", "fetch", 3, 10, 1)
        payload = await asyncio.wait_for(q.get(), timeout=2.0)
        assert payload["type"] == "sync.progress"
        assert payload["collection_id"] == "c1"
        assert payload["phase"] == "fetch"
        assert payload["done"] == 3 and payload["total"] == 10 and payload["errors"] == 1
        # 内存态（v0.51 轮询端点）与事件同一写点
        assert engine.get_progress("c1") == {"phase": "fetch", "done": 3, "total": 10, "errors": 1}
        events.unsubscribe(q)

    _run_with_bus(body)


def test_sse_generator_delivers_events():
    """流生成器直驱测试：retry/hello 帧、事件帧、aclose 后订阅清理。

    不经 TestClient 流式请求：其 ASGI transport 会缓冲整个响应体，无限流
    SSE 永远等不到响应头（死锁）——生成器独立成函数正是为此可直驱。
    """

    async def body(_loop):
        gen = events.sse_generator()
        first = await asyncio.wait_for(gen.__anext__(), 2.0)
        assert first == "retry: 3000\n\n"
        second = await asyncio.wait_for(gen.__anext__(), 2.0)
        assert "event: hello" in second and '"data":' not in second
        assert events.subscriber_count() == 1
        events.publish("kb.changed", stage="normalize", op="write")
        frame = await asyncio.wait_for(gen.__anext__(), 2.0)
        assert frame.startswith("event: kb.changed\n")
        assert '"stage": "normalize"' in frame  # ensure_ascii=False 且不转义
        await gen.aclose()
        assert events.subscriber_count() == 0  # finally 清理

    _run_with_bus(body)


def test_sse_endpoint_auth(cfg):
    cfg.data["token"] = "secret"
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/api/events").status_code == 401
        assert client.get("/api/events", headers={"X-KB-Token": "wrong"}).status_code == 401
