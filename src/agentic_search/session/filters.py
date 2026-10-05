"""Saved filters (filters/fN.json in the session): who matches one condition, computed once.

A result set is the intersection of its filters' `members`, so adding or removing a filter never
searches the index again. `members` are raw documents, every version of every CV; a set keeps the
newest CV of each person (query/view.py).

    kind = "criteria"   one condition from `search` / `filter` (its terms, or one constraint)
    kind = "frozen"     the members of an existing set (union / minus, or a set that records no filters)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pyroaring import BitMap

from ..errors import ResumesError
from .bitmap import decode, encode
from .store import Session, now_iso, write_atomic


@dataclass
class Filter:
    id: str
    kind: str                                   # criteria | frozen
    key: str                                    # the condition, canonical: the same key is the same filter
    label: str                                  # `skill=elixir`, `rate-max=80`, `rs_03`
    args: dict                                  # Criteria.to_args() of this condition (the user's words)
    index_version: str
    members: BitMap
    strong: BitMap                              # the part that rests on strong evidence
    order: list[int] | None = None              # its own ranking over `members`; None for a constraint (it ranks nothing)
    unknown_years: BitMap = field(default_factory=BitMap)   # documents a --min-years filter cannot decide
    unknown_rate: BitMap = field(default_factory=BitMap)    # documents a --rate-max filter cannot decide
    resolution: dict = field(default_factory=dict)          # resolved / expanded / text_fallback / like_* (what the set file records)
    echo: dict = field(default_factory=dict)                # what the header needs to say how the words were read
    said: str | None = None                                 # the user's own words, when the agent passed them
    created_at: str = ""

    @property
    def ranked(self) -> bool:
        return self.order is not None

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id, "kind": self.kind, "key": self.key, "label": self.label, "said": self.said, "args": self.args,
            "resolution": self.resolution, "echo": self.echo, "index_version": self.index_version, "count": len(self.members),
            "members": encode(self.members), "strong": encode(self.strong), "order": self.order,
            "unknown_years": encode(self.unknown_years), "unknown_rate": encode(self.unknown_rate), "created_at": self.created_at,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Filter":
        d = json.loads(text)
        return cls(
            id=d["id"], kind=d["kind"], key=d["key"], label=d["label"], args=d.get("args") or {}, index_version=d["index_version"],
            members=decode(d["members"]), strong=decode(d["strong"]), order=d.get("order"),
            unknown_years=decode(d["unknown_years"]) if d.get("unknown_years") else BitMap(),
            unknown_rate=decode(d["unknown_rate"]) if d.get("unknown_rate") else BitMap(),
            resolution=d.get("resolution") or {}, echo=d.get("echo") or {}, said=d.get("said"), created_at=d.get("created_at", ""),
        )


def load(session: Session, fid: str) -> Filter:
    p = session.filter_path(fid)
    if not p.is_file():
        have = ", ".join(session.filter_ids()) or "none"
        raise ResumesError("UNKNOWN_FILTER", f"{fid} (have {have})")
    return Filter.from_json(p.read_text(encoding="utf-8"))


def save(session: Session, f: Filter) -> Filter:
    if not f.created_at:
        f.created_at = now_iso()
    write_atomic(session.filter_path(f.id), f.to_json())
    session.remember_filter(f.key, f.id)
    return f


def find(session: Session, key: str) -> Filter | None:
    fid = session.filter_index().get(key)
    if fid is None or not session.filter_path(fid).is_file():
        return None
    return load(session, fid)


def frozen(session: Session, *, label: str, order: list[int], index_version: str, key: str | None = None) -> Filter:
    """A filter that is the members of an existing set, in that set's order."""
    key = key or f"frozen:{label}"
    old = find(session, key)
    if old is not None and old.order == list(order):
        return old
    bm = BitMap(order)
    return save(session, Filter(id=old.id if old else session.next_filter_id(), kind="frozen", key=key, label=label, args={},
                                index_version=index_version, members=bm, strong=BitMap(bm), order=list(order)))
