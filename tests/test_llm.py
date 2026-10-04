"""llm 客户端协议分支测试（§9 决策 4：OpenAI 兼容 + Ollama 原生，v0.18）。

回归：thinking 模型（qwen3/deepseek-r1）在 OpenAI 兼容端点先生成思维链，
长输出实测可超 180s 超时；Ollama 预设须走原生 /api/chat + think:false。
"""

import httpx
import json
import pytest

from kbserver.llm import LLMError, client_for_task


def test_openai_branch_request_shape():
    cfg = {
        "providers": {"glm": {"base_url": "http://x/v1", "model": "m1", "api_key_env": ""}},
        "tasks": {"tags": {"provider": "glm", "model": ""}},
        "timeout": 5,
    }
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    client = client_for_task(cfg, "tags")
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as _:
        # 直接替换内部 client 创建逻辑：通过 monkeypatch httpx.Client
        import kbserver.llm as llm_mod

        orig = llm_mod.httpx.Client
        llm_mod.httpx.Client = lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"})
        try:
            out = client.chat([{"role": "user", "content": "q"}])
        finally:
            llm_mod.httpx.Client = orig

    assert out == "hi"
    assert captured["url"].endswith("/chat/completions")
    assert captured["payload"]["model"] == "m1"
    assert "think" not in captured["payload"]


def test_ollama_branch_native_and_no_think():
    """Ollama 分支：原生 /api/chat、stream=false、think=false、解析 message.content。"""
    cfg = {
        "providers": {"ollama": {"base_url": "http://x/v1", "model": "qwen3:1.7b", "api_key_env": "", "api": "ollama"}},
        "tasks": {"tags": {"provider": "ollama", "model": ""}},
        "timeout": 5,
    }
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "你好"}})

    client = client_for_task(cfg, "tags")
    transport = httpx.MockTransport(handler)
    import kbserver.llm as llm_mod

    orig = llm_mod.httpx.Client
    llm_mod.httpx.Client = lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"})
    try:
        out = client.chat([{"role": "user", "content": "q"}])
    finally:
        llm_mod.httpx.Client = orig

    assert out == "你好"
    assert captured["url"].endswith("/api/chat")
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["think"] is False


def test_ollama_branch_http_error_masked():
    """错误响应只暴露状态码与截断响应体，不回显请求头。"""
    cfg = {
        "providers": {"ollama": {"base_url": "http://x/v1", "model": "m", "api_key_env": "", "api": "ollama"}},
        "tasks": {"tags": {"provider": "ollama", "model": ""}},
        "timeout": 5,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers  # api_key_env 为空不得带头
        return httpx.Response(404, json={"error": "model not found"})

    client = client_for_task(cfg, "tags")
    transport = httpx.MockTransport(handler)
    import kbserver.llm as llm_mod

    orig = llm_mod.httpx.Client
    llm_mod.httpx.Client = lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"})
    try:
        with pytest.raises(LLMError, match="404"):
            client.chat([{"role": "user", "content": "q"}])
    finally:
        llm_mod.httpx.Client = orig
