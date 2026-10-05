"""A result set as a function of the session's saved filters.

    members  = intersection of the filters' documents, among the newest CV of every person
    base     = the ranking of the first filter that has one, restricted to the members;
               no such filter → newest document first
    order    = the sort keys on top of the base order

Each condition is evaluated once (`ensure`) and saved, so adding or removing a filter is a bitmap AND.
"""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from pyroaring import BitMap

from ..config import Config
from ..errors import ResumesError
from ..session import filters as fmod
from ..session.filters import Filter
from ..session.store import ResultSet, Session, filter_number
from .index import Index
from .like import read_source
from .search import Criteria, Membership, describe_args, evaluate
from .sort import sort_order
from .terms import Resolved

RANK_WORDS = ("rank", "ranking", "sort", "order", "judgment", "judgement")


# ---------------------------------------------------------------- conditions → saved filters


def split(crit: Criteria) -> list[Criteria]:
    """The terms of one call as one filter, and each constraint as its own, so each can be removed alone."""
    parts: list[Criteria] = []
    if crit.has_terms:
        parts.append(Criteria(topics=list(crit.topics), skills=list(crit.skills), text=crit.text, mode=crit.mode, strict=crit.strict,
                              level_used=crit.level_used, like=crit.like, like_session_dir=crit.like_session_dir))
    if crit.min_years is not None:
        parts.append(Criteria(min_years=crit.min_years, include_unknown=crit.include_unknown))
    if crit.rate_max is not None:
        parts.append(Criteria(rate_max=crit.rate_max, include_unknown=crit.include_unknown))
    if crit.seniority:
        parts.append(Criteria(seniority=list(crit.seniority)))
    if crit.availability:
        parts.append(Criteria(availability=list(crit.availability)))
    if crit.location:
        parts.append(Criteria(location=list(crit.location)))
    if crit.remote is not None:
        parts.append(Criteria(remote=crit.remote))
    return parts


def condition_key(index: Index, part: Criteria) -> str:
    """Canonical text of a condition, so the same words reuse the same filter."""
    args = part.to_args()
    for k in ("topic", "skill", "seniority", "availability"):
        if k in args:
            args[k] = [" ".join(v.lower().split()) for v in args[k]]
    for k in ("seniority", "availability"):        # "any of these": order is irrelevant
        if k in args:
            args[k] = sorted(set(args[k]))
    for k in ("text", "location"):
        if isinstance(args.get(k), str):
            args[k] = " ".join(args[k].lower().split())
    if isinstance(args.get("location"), list):
        args["location"] = sorted({" ".join(v.lower().split()) for v in args["location"]})
    if part.like:                      # the file can be rewritten: the key follows its content
        _, text = read_source(index, part.like, part.like_session_dir)
        args["like"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return json.dumps(args, sort_keys=True, ensure_ascii=False)


def _echo(part: Criteria, m: Membership) -> dict:
    if not part.has_terms:
        return {}
    out: dict = {"terms": True, "mode": part.mode, "resolved": [dict(r.__dict__) for r in m.resolved], "unresolved": list(m.unresolved)}
    if m.text is not None:
        out["text"] = {"query": m.text.query, "words": list(m.text.words), "tau": m.text.tau}
    if m.like is not None:
        out["like"] = {"summary": m.like.summary, "min_cover": m.like.min_cover, "source": m.like.source,
                       "requirements": len(m.like.requirements), "resolved": list(m.like.resolved), "unresolved": list(m.like.unresolved)}
    return out


def _evaluate_into(index: Index, cfg: Config, part: Criteria, f: Filter) -> Filter:
    m = evaluate(index, cfg, part, collapse=False)
    f.index_version = index.version
    f.members, f.strong = m.members, m.strong
    f.order = list(m.order) if part.has_terms else None
    f.unknown_years, f.unknown_rate = m.unknown_years_bm, m.unknown_rate_bm
    f.resolution = m.resolution_args(index.vocab) if part.has_terms else {}
    f.echo = _echo(part, m)
    return f


def ensure(index: Index, cfg: Config, session: Session, part: Criteria, said: str | None = None) -> tuple[Filter, bool]:
    """(the saved filter for one condition, whether it had to be computed now)."""
    key = condition_key(index, part)
    old = fmod.find(session, key)
    if old is not None and old.index_version == index.version:
        if said and not old.said:
            old.said = said
            fmod.save(session, old)
        return old, False
    args = part.to_args()
    f = old or Filter(id="", kind="criteria", key=key, label=describe_args(args).replace("--remote", "remote"), args=args, index_version=index.version,
                      members=BitMap(), strong=BitMap(), said=said)
    _evaluate_into(index, cfg, part, f)          # raises before anything is written
    if not f.id:
        f.id = session.next_filter_id()
    return fmod.save(session, f), True


def refresh(index: Index, cfg: Config, session: Session, f: Filter) -> Filter:
    """Re-evaluate a filter saved against another index version."""
    if f.index_version == index.version:
        return f
    if f.kind == "criteria":
        part = Criteria.from_args(f.args)
        part.like_session_dir = session.dir
        _evaluate_into(index, cfg, part, f)
    else:
        keep = index.all_docs
        f.members, f.strong = f.members & keep, f.strong & keep
        f.order = [d for d in (f.order or []) if d in keep]
        f.index_version = index.version
    return fmod.save(session, f)


def filters_of(index: Index, cfg: Config, session: Session, rs: ResultSet) -> list[Filter]:
    """The filters a set is made of. An old-format set without filters becomes one frozen filter."""
    if rs.filters is None:
        return [fmod.frozen(session, label=rs.id, order=rs.order, index_version=rs.index_version)]
    return [refresh(index, cfg, session, fmod.load(session, fid)) for fid in rs.filters]


# ---------------------------------------------------------------- filters → members and order


def people(index: Index, docs) -> int:
    """How many people a set of documents is: a person is their newest CV."""
    return len(BitMap(docs) & index.representatives)


def raw_members(index: Index, filters: list[Filter]) -> BitMap:
    """The documents that pass every filter, one per person (a frozen filter already is)."""
    out = BitMap(index.all_docs) if any(f.kind == "frozen" for f in filters) else BitMap(index.representatives)
    for f in filters:
        out &= f.members
    return out


def anchor_of(filters: list[Filter]) -> Filter | None:
    return next((f for f in filters if f.ranked), None)


def base_order(index: Index, filters: list[Filter], members: BitMap) -> list[int]:
    anchor = anchor_of(filters)
    return sorted(members, reverse=True) if anchor is None else [d for d in anchor.order if d in members]


def parse_sort(sort: list[str]) -> list[tuple[str, str]]:
    return [(k.partition(":")[0], k.partition(":")[2] or "desc") for k in sort]


def view_key(index_version: str, filters: list[Filter], sort: list[str], jid: str | None, scores: dict | None) -> str:
    ids = sorted((f.id for f in filters), key=filter_number)
    anchor = anchor_of(filters)
    ranking = "-"
    if jid and any(k.startswith("judgment") for k in sort):
        ranking = f"{jid}@{sum(1 for s, _ in (scores or {}).values() if s is not None)}"
    return "|".join([index_version, "+".join(ids) or "all", f"by:{anchor.id if anchor else 'newest'}", ",".join(sort), ranking])


def materialize(index: Index, session: Session, filters: list[Filter], sort: list[str], jid: str | None = None,
                scores: dict | None = None) -> tuple[list[int], str | None, str]:
    """(order, id of the set this order was reused from or None, view key)."""
    key = view_key(index.version, filters, sort, jid, scores)
    hit = session.views().get(key)
    if hit and session.set_path(hit).is_file():
        return list(session.get(hit).order), hit, key
    base = base_order(index, filters, raw_members(index, filters))
    keys = parse_sort(sort)
    order = sort_order(index, base, keys, scores=scores, relevance=base) if keys else base
    return order, None, key


# ---------------------------------------------------------------- what the header says


def shown_membership(index: Index, filters: list[Filter], new: list[Filter], order: list[int]) -> Membership:
    members = BitMap(order)
    tf = next((f for f in new if f.echo.get("terms")), None)
    resolved = [Resolved(**r) for r in tf.echo["resolved"]] if tf else []
    unresolved = list(tf.echo["unresolved"]) if tf else []
    text = SimpleNamespace(**tf.echo["text"]) if tf and tf.echo.get("text") else None
    like = None
    if tf and tf.echo.get("like"):
        lk = dict(tf.echo["like"])
        lk["requirements"] = [None] * int(lk["requirements"])
        like = SimpleNamespace(**lk)
    strong = (tf.strong & members) if tf else BitMap(members)
    uy = ur = 0
    for f in new:
        if not (f.unknown_years or f.unknown_rate):
            continue
        others = raw_members(index, [g for g in filters if g.id != f.id])
        uy += people(index, others & f.unknown_years)
        ur += people(index, others & f.unknown_rate)
    return Membership(resolved, unresolved, text, members, strong, list(order), {}, uy, ur, {}, like)  # type: ignore[arg-type]


def resolution_of(new: list[Filter]) -> dict:
    tf = next((f for f in new if f.echo.get("terms")), None)
    return dict(tf.resolution) if tf else {"resolved": []}


# ---------------------------------------------------------------- which filter a word means


def match(filters: list[Filter], target: str) -> Filter:
    """`f2`, or a word of the filter (`elixir`, `rate`, `berlin`)."""
    listing = " · ".join(f"{f.id} {f.label}" for f in filters) or "none"
    t = target.strip().lower()
    if re.fullmatch(r"f_?\d+", t):
        fid = "f" + str(int(t.lstrip("f_")))
        for f in filters:
            if f.id == fid:
                return f
        raise ResumesError("UNKNOWN_FILTER", f"{fid} is not active (active: {listing})")
    words = [w for w in re.split(r"[^a-z0-9+#.]+", t) if w]
    hits = []
    for f in filters:
        hay = " ".join([f.label, f.said or "", " ".join(f.resolution.get("resolved", [])), json.dumps(f.args, ensure_ascii=False)]).lower()
        hay_words = set(re.split(r"[^a-z0-9+#.]+", hay))
        if words and all(w in hay_words or any(h.startswith(w) for h in hay_words if len(w) >= 3) for w in words):
            hits.append(f)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ResumesError("UNKNOWN_FILTER", f'"{target}" matches no active filter (active: {listing})')
    raise ResumesError("AMBIGUOUS_FILTER", f'"{target}" matches {", ".join(f.id for f in hits)}: say which (active: {listing})')
