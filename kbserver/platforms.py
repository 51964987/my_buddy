"""平台识别与 source_type 分流（§4.2 枚举）。新增平台 = 注册新值，不改管道。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_RULES: list[tuple[str, str, str]] = [
    (r"(^|\.)(xiaohongshu\.com|xhslink\.com)$", "xiaohongshu", "social"),
    (r"(^|\.)(douyin\.com|iesdouyin\.com)$", "douyin", "social"),
    (r"(^|\.)(kuaishou\.com|chenzhongtech\.com)$", "kuaishou", "social"),
    (r"(^|\.)(feishu\.cn|feishu\.com|larksuite\.com)$", "feishu", "collab_doc"),
    (r"(^|\.)docs\.qq\.com$", "tencent_doc", "collab_doc"),
    (r"(^|\.)(notion\.so|notion\.site)$", "notion", "collab_doc"),
    (r"(^|\.)yuque\.com$", "yuque", "collab_doc"),
    (r"(^|\.)docs\.volcengine\.com$", "volcengine", "product_doc"),
]

_RULES_COMPILED = [(re.compile(pattern), platform, source_type) for pattern, platform, source_type in _RULES]


def detect_platform(url: str) -> tuple[str, str]:
    host = (urlsplit(url).hostname or "").lower()
    for rx, platform, source_type in _RULES_COMPILED:
        if rx.search(host):
            return platform, source_type
    return "web", "social"


def source_type_options() -> list[str]:
    """source_type 固定枚举（§4.2）：前端过滤器选项来源（UI 规则：禁止前端硬编码）。"""
    return sorted({source_type for _rx, _platform, source_type in _RULES_COMPILED})


def platform_options() -> list[str]:
    """platform 注册表值（开放枚举，§4.2）：新增平台 = 注册新值。"""
    return sorted({platform for _rx, platform, _st in _RULES_COMPILED})
