"""A directory of *.md files with optional YAML front matter → RawDoc (the fixture corpus, the inbox).

Front matter keys pass through in `meta` (rate, currency, location, remote, availability, category …).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from .types import RawDoc

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def split_front_matter(text: str) -> tuple[dict, str]:
    m = _FRONT.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[m.end():]


def load(path: Path, source_id: str) -> Iterator[RawDoc]:
    if not path.is_dir():
        return
    for file in sorted(path.glob("*.md")):
        meta, body = split_front_matter(file.read_text(encoding="utf-8"))
        category = meta.get("category")
        yield RawDoc(
            source=source_id,
            source_id=file.stem,
            category=str(category) if category else None,
            markdown=body,
            meta={"converter": "markdown", **meta},
        )
