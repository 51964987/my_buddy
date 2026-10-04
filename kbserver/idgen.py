"""ID/去重引擎（§9 决策 1）：URL 规范化 → SHA-1 前 12 位。

capture 阶段以提交 URL 生成临时 id；normalize 解析重定向后按 canonical URL
生成最终 id（首次落盘后永不改变）。无 URL 文本条目降级为 时间戳+短随机。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEY_PREFIXES = ("utm_", "share_")
TRACKING_KEYS = {"spm", "vd_source", "vd_extension", "fr", "ref", "refer", "_vtm_"}

ID_LENGTH = 12


def _is_tracking_key(key: str) -> bool:
    k = key.lower()
    return k.startswith(TRACKING_KEY_PREFIXES) or k in TRACKING_KEYS


def canonicalize_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return url
    host = parts.hostname.lower()
    port = parts.port
    default = {"http": 80, "https": 443}.get(parts.scheme)
    netloc = host if (port is None or port == default) else f"{host}:{port}"
    path = parts.path or "/"
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_key(k)
    )
    return urlunsplit((parts.scheme, netloc, path, urlencode(query), ""))


def url_to_id(url: str) -> str:
    return hashlib.sha1(canonicalize_url(url).encode("utf-8")).hexdigest()[:ID_LENGTH]


def text_entry_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"t{ts}-{secrets.token_hex(3)}"


def collection_page_id(collection_id: str, site_path: str) -> str:
    """D 类页面 id（§9 决策 1）：SHA-1(collection-id + 站内路径) 前 12 位。

    站内路径取 CanonicalURL 路径（文档站页面 URL 查询参数不稳定，不入哈希），
    跨同步稳定不变；同页面重复同步天然幂等。
    """
    return hashlib.sha1(f"{collection_id}:{site_path}".encode("utf-8")).hexdigest()[:ID_LENGTH]
