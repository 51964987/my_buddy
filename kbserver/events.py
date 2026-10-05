"""进程内事件总线（§11.8 ⑥ v0.53）：写边界/同步进度的轻量 pub/sub，供 SSE 推送。

设计口径：
- 只发「变更通知」不发内容——`kb.changed` 不携带落盘数据，前端收到后 debounce
  重拉既有聚合端点（/api/status + /api/wiki）。避免 SSE 与全库扫描出现两套聚合
  口径（口径漂移），也不把每次写盘的内容塞进事件。
- 线程安全：事件多产生自工作线程（orchestrator enrich worker / sync 线程 /
  请求处理线程），publish 经 loop.call_soon_threadsafe 投递到主事件循环；
  loop 未注入（测试环境/未启动）或无订阅者时 no-op，零成本。
- 队列满丢最旧：kb.changed 是幂等触发（重拉全量），丢事件不致状态错误；
  重连（hello）后整体重拉一次兜底。
- publish 绝不向调用方抛异常——事件通知是旁路可观测能力，不得影响写盘主流程。
"""

from __future__ import annotations

import asyncio
import json
import time

# 单订阅者队列上限：满则丢最旧（幂等触发语义下安全，见模块 docstring）
QUEUE_SIZE = 256
# SSE 心跳间隔（秒）：保活 + 让代理/客户端确认连接存活
SSE_HEARTBEAT_SECONDS = 15.0

_loop: asyncio.AbstractEventLoop | None = None
_subscribers: set[asyncio.Queue] = set()


def set_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """由 app lifespan 注入主事件循环；None 表示停用（关停/测试清理）。"""
    global _loop
    _loop = loop


def subscriber_count() -> int:
    return len(_subscribers)


def publish(event_type: str, **data: object) -> None:
    """发布事件（任意线程可调）。无 loop / 无订阅者时 no-op；绝不抛出。"""
    loop = _loop
    if loop is None or not _subscribers:
        return
    payload = {"type": event_type, "ts": time.time(), **data}

    def _deliver() -> None:
        for q in list(_subscribers):
            if q.full():
                try:
                    q.get_nowait()  # 丢最旧
                except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                    pass
            q.put_nowait(payload)

    try:
        loop.call_soon_threadsafe(_deliver)
    except RuntimeError:
        # loop 已关闭（进程退出竞态）：事件通知可丢，静默
        pass


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


async def sse_generator():
    """SSE 事件流生成器（app.py 以 StreamingResponse 包装；§11.8 ⑥ v0.53）。

    帧：retry（重连建议）→ hello（重连后整体重拉的触发信号）→ 事件帧 → 15s 心跳。
    客户端断开由框架取消本生成器任务（不主动轮询 is_disconnected：该调用在
    ASGI 测试 transport 下会阻塞等 disconnect 消息形成死锁），CancelledError
    穿透 finally 完成订阅清理。生成器独立成函数使其可被 asyncio 直接驱动
    （TestClient 的 ASGI transport 会缓冲整个响应体，无法测无限流）。
    """
    q = await subscribe()
    try:
        yield "retry: 3000\n\n"
        yield "event: hello\ndata: {}\n\n"
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            data = json.dumps(ev, ensure_ascii=False)
            yield f"event: {ev.get('type', 'message')}\ndata: {data}\n\n"
    finally:
        unsubscribe(q)
