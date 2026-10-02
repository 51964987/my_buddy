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
    },
    "ai": {},
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
        before = (self.data.get("host"), self.data.get("port"))
        _deep_merge(self.data, _strip_masks(copy.deepcopy(patch), self.data))
        after = (self.data.get("host"), self.data.get("port"))
        return after != before
