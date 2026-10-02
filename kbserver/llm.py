"""OpenAI 兼容 LLM 客户端（§9 决策 4）。

统一协议调用任一 OpenAI 兼容后端（云端 API / Ollama / vLLM）。
api_key 严格 env-only：只按 provider 条目记录的环境变量名在运行时读取，
永不落盘、永不打印（安全红线）。换后端只改配置，不改代码。
"""

from __future__ import annotations

import os

import httpx


class LLMError(Exception):
    """LLM 调用失败（网络/协议/响应格式），由调用方决定重试或转 error。"""


class ChatClient:
    """单次 chat/completions 调用的最小客户端。"""

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def chat(self, messages: list[dict], temperature: float = 0.2) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "messages": messages, "temperature": temperature}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            # 只暴露状态码与截断的响应体，绝不回显请求头（含 key）
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected response shape: {type(exc).__name__}") from exc


def client_for_task(ai_cfg: dict, task: str) -> ChatClient:
    """按任务分级解析 provider/model 并实例化客户端（§11.4 配置中心分发）。

    model 为空串时回落到 provider 默认 model；
    api_key_env 为空（如 Ollama）或环境变量未设置时 key 取空串。
    """
    providers = ai_cfg.get("providers") or {}
    tasks = ai_cfg.get("tasks") or {}
    tcfg = tasks.get(task)
    if not tcfg or not tcfg.get("provider"):
        raise LLMError(f"task '{task}' has no provider configured")
    pname = tcfg["provider"]
    pcfg = providers.get(pname)
    if not pcfg or not pcfg.get("base_url"):
        raise LLMError(f"provider '{pname}' is not configured")
    model = (tcfg.get("model") or pcfg.get("model") or "").strip()
    if not model:
        raise LLMError(f"task '{task}' resolved to empty model")
    env_name = (pcfg.get("api_key_env") or "").strip()
    api_key = os.environ.get(env_name, "") if env_name else ""
    timeout = float(ai_cfg.get("timeout", 60.0))
    return ChatClient(pcfg["base_url"], model, api_key, timeout=timeout)
