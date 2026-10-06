"""OpenAI 兼容 LLM 客户端（§9 决策 4）。

统一协议调用任一 OpenAI 兼容后端（云端 API / Ollama / vLLM）。
api_key 严格 env-only：只按 provider 条目记录的环境变量名在运行时读取，
永不落盘、永不打印（安全红线）。换后端只改配置，不改代码。
"""

from __future__ import annotations

import json
import os

import httpx


class LLMError(Exception):
    """LLM 调用失败（网络/协议/响应格式），由调用方决定重试或转 error。"""


def resolve_task(ai_cfg: dict, task: str) -> dict:
    """按任务分级解析 provider/model/key（§11.4 配置中心分发，chat 与 embedding 共用）。

    model 为空串时回落到 provider 默认 model；
    api_key_env 为空（如 Ollama）或环境变量未设置时 key 取空串。
    provider 可选 `api: "ollama"`（v0.18）：ChatClient 走 Ollama 原生 /api/chat
    并关 thinking——qwen3/deepseek-r1 等 thinking 模型在 OpenAI 兼容端点会先生成
    思维链，实测长输出可超 180s 超时（txxy 项目同款踩坑），think:false 后快约 3 倍。
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
    return {
        "base_url": pcfg["base_url"],
        "model": model,
        "api_key": api_key,
        "timeout": float(ai_cfg.get("timeout", 60.0)),
        "dimensions": tcfg.get("dimensions"),
        "api": (pcfg.get("api") or "openai").strip(),
    }


class ChatClient:
    """单次 chat 调用的最小客户端（api="openai" 走兼容协议；"ollama" 走原生并关 thinking）。"""

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 60.0, api: str = "openai"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.api = api

    def chat(self, messages: list[dict], temperature: float = 0.2) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.api == "ollama":
            # Ollama 原生协议：think:false 跳过思维链（thinking 模型不关会在兼容端点拖到超时）。
            # base_url 允许带 /v1（兼容端点语义），原生路径须剥掉
            base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
            path, payload = "/api/chat", {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "options": {"temperature": temperature},
            }
            pick = lambda data: data["message"]["content"]  # noqa: E731
        else:
            base = self.base_url
            path, payload = "/chat/completions", {"model": self.model, "messages": messages, "temperature": temperature}
            pick = lambda data: data["choices"][0]["message"]["content"]  # noqa: E731
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(f"{base}{path}", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            # 只暴露状态码与截断的响应体，绝不回显请求头（含 key）
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return pick(resp.json())
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected response shape: {type(exc).__name__}") from exc


    def chat_stream(self, messages: list[dict], temperature: float = 0.2):
        """流式 chat：逐段 yield 文本增量（同步生成器，异步侧由调用方桥接）。

        协议分支与 chat() 同源：
        - openai 兼容：SSE `data: {...}` 行取 choices[0].delta.content，`[DONE]` 结束；
        - ollama 原生：NDJSON 逐行 JSON 取 message.content，`done: true` 结束（think:false 保持）。
        非零状态码抛 LLMError（流模式下先 read() 取响应体再判错）；坏行跳过不中断流。
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.api == "ollama":
            base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
            path, payload = "/api/chat", {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "think": False,
                "options": {"temperature": temperature},
            }
        else:
            base = self.base_url
            path, payload = "/chat/completions", {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", f"{base}{path}", headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        # 只暴露状态码与截断的响应体，绝不回显请求头（含 key）
                        body = resp.read().decode("utf-8", errors="replace")
                        raise LLMError(f"HTTP {resp.status_code}: {body[:200]}")
                    for line in resp.iter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if self.api == "ollama":
                            # NDJSON：每行一个 JSON 对象
                            try:
                                data = json.loads(line)
                            except ValueError:
                                continue
                            if data.get("done"):
                                break
                            delta = (data.get("message") or {}).get("content")
                            if delta:
                                yield delta
                        else:
                            # SSE：`data: {...}` / `data: [DONE]`
                            if not line.startswith("data:"):
                                continue
                            data_str = line[len("data:"):].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                            except ValueError:
                                continue
                            choices = data.get("choices") or []
                            delta = ((choices[0].get("delta") or {}).get("content")) if choices else None
                            if delta:
                                yield delta
        except httpx.HTTPError as exc:
            raise LLMError(f"request failed: {type(exc).__name__}") from exc


class EmbedClient:
    """单次 /embeddings 调用的最小客户端（OpenAI 兼容，§5.1 v0.17 向量增强）。"""

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 60.0, dimensions: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.dimensions = int(dimensions) if dimensions else None

    def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict = {"model": self.model, "input": texts}
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(f"{self.base_url}/embeddings", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()["data"]
            # 按 index 排序，保证与输入顺序一致（协议允许乱序返回）
            return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {type(exc).__name__}") from exc


def client_for_task(ai_cfg: dict, task: str) -> ChatClient:
    """按任务分级解析并实例化 chat 客户端。"""
    r = resolve_task(ai_cfg, task)
    return ChatClient(r["base_url"], r["model"], r["api_key"], timeout=r["timeout"], api=r["api"])


def embed_client_for_task(ai_cfg: dict, task: str = "embedding") -> EmbedClient:
    """按任务分级解析并实例化 embedding 客户端（dimensions 随任务配置）。"""
    r = resolve_task(ai_cfg, task)
    return EmbedClient(r["base_url"], r["model"], r["api_key"], timeout=r["timeout"], dimensions=r["dimensions"])
