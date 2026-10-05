"""`sort SET --by KEY[,KEY…]`. Missing values sort last whatever the direction."""

from __future__ import annotations

from ..errors import ResumesError
from .facets import seniority_rank
from .index import Index, Profile

DEFAULT_DIR = {"judgment": "desc", "rate": "asc", "years": "desc", "relevance": "desc", "seniority": "desc"}


def parse_keys(text: str) -> list[tuple[str, str]]:
    keys = []
    for raw in text.split(","):
        raw = raw.strip().lower()
        if not raw:
            continue
        name, _, direction = raw.partition(":")
        if name not in DEFAULT_DIR:
            raise ResumesError("BAD_SORT_KEY", f"{name!r}: use {', '.join(DEFAULT_DIR)}")
        direction = direction or DEFAULT_DIR[name]
        if direction not in ("asc", "desc"):
            raise ResumesError("BAD_SORT_KEY", f"{raw!r}: direction must be asc or desc")
        keys.append((name, direction))
    if not keys:
        raise ResumesError("BAD_SORT_KEY", "--by needs at least one key")
    return keys


def sort_order(index: Index, order: list[int], keys: list[tuple[str, str]], *, scores: dict[int, tuple[int | None, str]] | None,
               relevance: list[int]) -> list[int]:
    profiles: dict[int, Profile] = index.profiles(order) if any(k in ("rate", "years", "seniority") for k, _ in keys) else {}
    rel_rank = {d: i for i, d in enumerate(relevance)}

    def value(d: int, key: str):
        if key == "judgment":
            return scores.get(d, (None, ""))[0] if scores else None
        p = profiles.get(d)
        if key == "rate":
            return p.rate if p else None
        if key == "years":
            return p.years if p else None
        if key == "seniority":
            return seniority_rank(p.seniority) if p else None
        if key == "relevance":
            r = rel_rank.get(d)
            return -r if r is not None else None      # earlier in the stage-one order = more relevant
        return None

    def sort_key(d: int):
        parts = []
        for key, direction in keys:
            v = value(d, key)
            if v is None:
                parts.append((1, 0.0))
            else:
                parts.append((0, -float(v) if direction == "desc" else float(v)))
        parts.append((0, float(rel_rank.get(d, d))))   # stable tie-break: keep the stage-one order
        return tuple(parts)

    return sorted(order, key=sort_key)
