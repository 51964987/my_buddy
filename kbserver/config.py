"""配置中心：服务参数唯一读写后端（§11.5）。

配置文件不属于 kb/ 库文件，不经过写边界守卫，但写盘一律原子写；
敏感项（token/api_key）对外输出一律掩码，回传掩码值视为"保持原值"。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

MASK = "******"
SENSITIVE_KEYS = {"token", "api_key"}

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}

# 自动整理触发模式（§5.1 v0.29）：manual=只响应手动指令（默认），auto=worker 周期自动扫库。
# 非法值在配置写入时即报错，避免"拼错 → 静默永不自动跑"这类失效。
TRIGGER_MODES = ("manual", "auto")

DEFAULTS: dict[str, Any] = {
    "kb_root": "",
    "host": "127.0.0.1",
    "port": 8765,
    "token": "",
    "pipeline": {
        "worker_enabled": True,
        "poll_interval": 2.0,
        "max_attempts": 3,
    },
    "normalize": {
        "image_localization": True,
        "max_images": 30,
        "fetch_timeout": 20.0,
        # 动态页兜底（§5.1 v0.17）：无选中文本且抓取/提取失败时经 Playwright 渲染重试
        "playwright_fallback": True,
        "playwright_timeout": 15.0,
    },
    "ai": {
        # enabled=False 时管道不调用任何 LLM；用户配置好 provider/环境变量后再开启
        "enabled": False,
        # provider 表（§9 决策 4）：api_key 不落盘，只记环境变量名，运行时从 os.environ 读取。
        # 默认走 Ollama 本地（v0.18：零成本、零凭据，开箱即用）；GLM 等云端后端按需切换。
        # api:"ollama" = 原生 /api/chat + think:false（thinking 模型不关会在兼容端点拖到超时）
        "providers": {
            "ollama": {
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen2.5:1.5b",
                "api_key_env": "",
                "api": "ollama",
            },
            "glm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key_env": "ZHIPUAI_API_KEY",
            },
        },
        # 任务分级（§11.5）：各任务独立 provider + model，页面可分别切换；
        # 默认标签/摘要卡均指 ollama 本地；
        # entity_extraction（§9 决策 6 v0.40）：实体抽取（知识图谱），默认 ollama 本地起步；
        # concept_card（v0.56 接入）：concept 聚合（aggregate stage）任务槽，
        # 默认未配置 = 聚合整体跳过（用户显式配置模型后才执行）；
        # embedding 为向量增强任务槽（§5.1 v0.17）：OpenAI 兼容 /embeddings，
        # 默认仍 glm（本地 Ollama 无 embedding 模型，且 dimensions 口径须与建库时一致）
        "tasks": {
            "tags": {"provider": "ollama", "model": ""},
            "summary_card": {"provider": "ollama", "model": ""},
            "entity_extraction": {"provider": "ollama", "model": ""},
            "concept_card": {"provider": "", "model": ""},
            "embedding": {"provider": "glm", "model": "embedding-3", "dimensions": 2048},
        },
        # 图谱 schema 白名单（§9 决策 6 v0.40）：全局一份，设置页可编辑。
        # 抽取输出受此约束（越界类型/孤儿关系直接丢弃）；entity_types 为空 = 未定义
        # schema，无从约束抽取，跳过实体抽取任务（不发起 LLM 调用）。
        # v0.58：默认不含「概念」——概念层知识由概念卡（aggregate 聚合）承担，
        # 实体层的「概念」类型与概念卡职责重叠，且是泛词重灾区（实测 glm-4-flash
        # 把正文高频普通名词「缺损/冲突/表状态」全塞进概念类实体）
        "kg": {
            "entity_types": ["产品", "技术", "组织", "人物", "地点", "工具", "事件"],
            "relation_types": ["属于", "包含", "依赖", "相关", "用于", "对比", "替代"],
        },
        # 默认 Ollama 本地推理较慢（4B 模型完整 prompt 可超 60s），超时默认放宽（v0.18）
        "timeout": 180.0,
        "max_attempts": 3,
        "poll_interval": 5.0,
        "batch_size": 5,
        # enrich worker 批内并发上限（§11.5 并发度参数）
        "concurrency": 1,
        # 触发与开关解耦（§5.1 v0.29）：enabled=AI 总闸（允许任何 LLM 调用，含试跑）；
        # trigger_mode 决定 worker 是否周期自动扫库。默认 manual——开总闸不等于自动全量跑。
        "trigger_mode": "manual",
        # 熔断（仅 auto 生效）：整轮全失败连续达阈值即静默停自动扫描，
        # 运行态 breaker 由程序写、经配置中心原子写（恢复只经显式 reset 动作，不自动恢复）
        "breaker_threshold": 3,
        "breaker": {
            "state": "closed",
            "consecutive_failures": 0,
            "opened_at": None,
            "last_error": None,
        },
    },
    # 索引分区（§11.5）：向量增强默认关闭（embedding 调用涉外部费用）
    "index": {
        "vector_enabled": False,
    },
}


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "kbserver.config.json"


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _deep_merge(base: dict, patch: dict) -> dict:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _merge_patch(target: dict, patch: dict) -> None:
    """PUT /api/config 补丁合并（§11.5 v0.19）：深合并，但值为 None 的键表示删除。

    动态键集合（如 ai.providers）需支持条目删除，深合并本身做不到——
    前端删除 provider 时以 {"ai": {"providers": {"<name>": null}}} 表达。
    """
    for k, v in patch.items():
        if v is None:
            target.pop(k, None)
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            _merge_patch(target[k], v)
        else:
            target[k] = v


def _strip_masks(patch: dict, current: dict) -> dict:
    out: dict = {}
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(current.get(k), dict):
            out[k] = _strip_masks(v, current[k])
        elif k in SENSITIVE_KEYS and v == MASK:
            continue
        else:
            out[k] = v
    return out


def _mask(obj: dict) -> dict:
    out: dict = {}
    for k, v in obj.items():
        if isinstance(v, dict):
            out[k] = _mask(v)
        elif k in SENSITIVE_KEYS and v:
            out[k] = MASK
        else:
            out[k] = v
    return out


def _validate_trigger_mode(patch: dict) -> None:
    """ai.trigger_mode 写入前枚举校验（§5.1 v0.29）。

    手改配置文件或前端下拉传错值时立刻报错，避免拼错导致"静默永不自动跑"。
    """
    ai_patch = patch.get("ai")
    if not isinstance(ai_patch, dict) or "trigger_mode" not in ai_patch:
        return
    mode = ai_patch["trigger_mode"]
    if mode not in TRIGGER_MODES:
        raise ValueError(f"invalid ai.trigger_mode: {mode!r} (allowed: {', '.join(TRIGGER_MODES)})")


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_config_path()
        self.data = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"config file must be a JSON object: {self.path}")
            _deep_merge(self.data, loaded)

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    @property
    def kb_root(self) -> Path:
        root = self.data.get("kb_root") or self.path.parent / "kb"
        return Path(root)

    def masked(self) -> dict:
        return _mask(self.data)

    def apply_update(self, patch: dict) -> bool:
        clean = _strip_masks(copy.deepcopy(patch), self.data)
        _validate_trigger_mode(clean)  # 合并前拦：非法枚举不留脏内存状态、也不落盘
        before = (self.data.get("host"), self.data.get("port"))
        _merge_patch(self.data, clean)
        after = (self.data.get("host"), self.data.get("port"))
        return after != before

    def write_breaker(self, **fields: Any) -> dict:
        """程序侧写熔断运行态（§5.1 v0.29）：只改 ai.breaker 子字段并原子写盘。

        与人工配置 PUT /api/config 分离：运行态不经掩码/深合并逻辑，也不因
        人工编辑其他分区被覆盖（不同键互不干扰）。恢复只经显式 reset 动作。
        """
        ai = self.data.setdefault("ai", {})
        breaker = dict(ai.get("breaker") or {})
        breaker.update(fields)
        ai["breaker"] = breaker
        self.save()
        return breaker


def check_listen_security(cfg: Config) -> None:
    host = str(cfg.data.get("host") or "")
    if host in LOOPBACK_HOSTS:
        return
    if not (cfg.data.get("token") or "").strip():
        raise RuntimeError(
            f"host '{host}' is non-loopback: token must be configured "
            "(safety rule: LAN listening requires token; desktop clients should use 127.0.0.1)"
        )
