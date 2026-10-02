"""note.md frontmatter 读写（§4.2 字段）。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def split_note(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    data = yaml.safe_load(m.group(1)) or {}
    if not isinstance(data, dict):
        return {}, text
    return data, text[m.end():]


def read_frontmatter(path: Path) -> dict:
    return split_note(path.read_text(encoding="utf-8"))[0]
