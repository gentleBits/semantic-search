"""Membership and stage-one order.

    M(t)     = topic: strong ∪ weak · skill: strong · --strict: strong · --loose: strong ∪ weak · --level used: used
    terms    = --all: ⋂ M(t) · --any: ⋃ M(t) · --min-cover k: docs in ≥ k of the M(t)
    members  = terms ∩ text channel ∩ constraints ∩ universe
    order    = coverage ↓, then reciprocal-rank fusion (k = 60) over evidence rank, BM25 and cosine
"""

from __future__ import annotations

import re

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pyroaring import BitMap

from ..config import Config
from .index import Index
from .like import LikeResult, analyse, read_source
from .terms import Resolved, expansion, resolve_phrases
from .text import TextResult, search_text

RRF_K = 60


@dataclass
class Criteria:
    topics: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    text: str | None = None
    mode: str | int = "all"                # "all" | "any" | k
    strict: bool | None = None             # True = --strict, False = --loose, None = the term's kind decides
    min_years: float | None = None
    seniority: list[str] = field(default_factory=list)
    rate_max: float | None = None
    location: list[str] = field(default_factory=list)
    remote: bool | None = None
    availability: list[str] = field(default_factory=list)   # now, 2w, 1m, 3m: the labels the facets show
    level_used: bool = False
    include_unknown: bool = False
    like: str | None = None                # a job description file or a document id
    like_session_dir: Path | None = None   # where a bare --like file name is looked up first

    @property
    def has_terms(self) -> bool:
        return bool(self.topics or self.skills or self.text or self.like)

    @property
    def has_explicit_terms(self) -> bool:
        return bool(self.topics or self.skills or self.text)

    def __post_init__(self) -> None:
        self.location = places(self.location)

    @property
    def has_constraints(self) -> bool:
        return any(v is not None for v in (self.min_years, self.rate_max, self.remote)) or bool(self.location) or bool(self.seniority) \
            or bool(self.availability) or self.level_used

    def to_args(self) -> dict:
        """What the set file records: the user's words, not their resolution."""
        out: dict = {}
        if self.topics:
            out["topic"] = list(self.topics)
        if self.skills:
            out["skill"] = list(self.skills)
        if self.text:
            out["text"] = self.text
        if self.mode != "all":
            out["mode"] = self.mode
        if self.strict is not None:
            out["strict"] = self.strict
        for k in ("min_years", "rate_max", "remote"):
            if getattr(self, k) is not None:
                out[k] = getattr(self, k)
        if self.location:                  # one place stays a string, so older saved filters still compare equal
            out["location"] = self.location[0] if len(self.location) == 1 else list(self.location)
        if self.seniority:
            out["seniority"] = list(self.seniority)
        if self.availability:
            out["availability"] = [availability_label(a) for a in self.availability]
        if self.level_used:
            out["level"] = "used"
        if self.include_unknown:
            out["include_unknown"] = True
        if self.like:
            out["like"] = self.like
        return out

    @classmethod
    def from_args(cls, args: dict) -> "Criteria":
        """The inverse of `to_args`."""
        return cls(
            topics=list(args.get("topic") or []), skills=list(args.get("skill") or []), text=args.get("text"),
            mode=args.get("mode", "all"), strict=args.get("strict"), min_years=args.get("min_years"),
            seniority=list(args.get("seniority") or []), rate_max=args.get("rate_max"), location=args.get("location"),
            remote=args.get("remote"), availability=list(args.get("availability") or []), level_used=args.get("level") == "used",
            include_unknown=bool(args.get("include_unknown")),
            like=args.get("like"),
        )


def place_pattern(place: str) -> str:
    """The place as whole words, any case: "Poland" is in "Krakow, Poland", "NY" is not in "Germany"."""
    return r"(^|[^a-z0-9])" + re.escape(place.lower()) + r"([^a-z0-9]|$)"


def place_matches(place: str, location: str | None) -> bool:
    return bool(location) and re.search(place_pattern(place), location.lower()) is not None


def places(v) -> list[str]:
    if v is None:
        return []
    raw = [v] if isinstance(v, str) else list(v)
    out = []
    for x in raw:
        x = " ".join(str(x).split())
        if x and x not in out:
            out.append(x)
    return out


def availability_label(a: str | None) -> str:
    """`immediately` → now · `2 weeks` → 2w · `1 month` → 1m."""
    from ..index.cards import _availability

    return (_availability(a) or "").removeprefix("avail ").strip()


def describe_args(args: dict) -> str:
    """`topic=data-pipelines skill=elixir min-years=5` for state lines and the sets tree."""
    parts = []
    for k in ("topic", "skill"):
        for v in args.get(k, []):
            parts.append(f"{k}={v.replace(' ', '-') if ' ' in v else v}")
    if args.get("text"):
        parts.append(f'text="{args["text"]}"')
    if args.get("mode") not in (None, "all"):
        m = args["mode"]
        parts.append(f"--{m}" if isinstance(m, str) else f"--min-cover {m}")
    if args.get("strict") is True:
        parts.append("--strict")
    if args.get("strict") is False:
        parts.append("--loose")
    for k, flag in (("min_years", "min-years"), ("rate_max", "rate-max"), ("location", "location")):
        if args.get(k) is not None:
            parts.append(f"{flag}={args[k]:g}" if isinstance(args[k], (int, float)) else f"{flag}={args[k]}")
    if args.get("seniority"):
        parts.append("seniority=" + ",".join(args["seniority"]))
    if args.get("availability"):
        parts.append("availability=" + ",".join(args["availability"]))
    if args.get("remote"):
        parts.append("--remote")
    if args.get("level"):
        parts.append(f"level={args['level']}")
    if args.get("like"):
        parts.append(f"like={Path(args['like']).name}")
    if args.get("by"):
        parts.append(",".join(args["by"]))
    if args.get("removed"):
        parts.append(", ".join(args["removed"]))
    if args.get("with") and args.get("algebra"):
        parts.append(args["algebra"])
    return " ".join(parts)


@dataclass
class Membership:
    resolved: list[Resolved]
    unresolved: list[str]
    text: TextResult | None
    members: BitMap
    strong: BitMap
    order: list[int]
    coverage: dict[int, int]
    unknown_years: int = 0
    unknown_rate: int = 0
    versions: dict[int, int] = field(default_factory=dict)      # doc_no → other versions collapsed into it
    like: LikeResult | None = None
    unknown_years_bm: BitMap = field(default_factory=BitMap)    # members a --min-years constraint cannot decide
    unknown_rate_bm: BitMap = field(default_factory=BitMap)     # members a --rate-max constraint cannot decide

    @property
    def count(self) -> int:
        return len(self.order)

    @property
    def weak_count(self) -> int:
        return len(self.members) - len(self.strong)

    def resolution_args(self, vocab) -> dict:
        out: dict = {"resolved": [r.slug for r in self.resolved]}
        exp = {r.slug: expansion(vocab, r.slug) for r in self.resolved}
        exp = {k: v for k, v in exp.items() if v}
        if exp:
            out["expanded"] = exp
        if self.unresolved:
            out["text_fallback"] = list(self.unresolved)
        if self.like is not None:
            out["like_resolved"] = list(self.like.resolved)
            out["like_requirements"] = len(self.like.requirements)
            out["like_unresolved"] = list(self.like.unresolved)
        return out


def combine(bitmaps: list[BitMap], mode: str | int) -> BitMap:
    if not bitmaps:
        return BitMap()
    if mode == "all":
        out = BitMap(bitmaps[0])
        for b in bitmaps[1:]:
            out &= b
        return out
    if mode == "any":
        out = BitMap()
        for b in bitmaps:
            out |= b
        return out
    k = int(mode)
    c: Counter[int] = Counter()
    for b in bitmaps:
        c.update(b)
    return BitMap(d for d, n in c.items() if n >= k)


def term_bitmaps(index: Index, r: Resolved, crit: Criteria) -> tuple[BitMap, BitMap]:
    """(membership, strong part) of one resolved term under the query's evidence rule."""
    strong, used, weak = index.bitmaps(r.slug)
    if crit.level_used:
        return used, used
    if crit.strict is True:
        return strong, strong
    if crit.strict is False or r.kind == "topic":
        return strong | weak, strong
    return strong, strong


def constraints(index: Index, crit: Criteria) -> tuple[BitMap, BitMap, BitMap] | None:
    """(docs passing every constraint, docs blocked only by unknown years, docs blocked only by unknown rate)."""
    if not any(v is not None for v in (crit.min_years, crit.rate_max, crit.remote)) and not crit.location and not crit.seniority and not crit.availability:
        return None
    where, params = ["true"], []
    if crit.seniority:
        where.append("lower(seniority) IN (SELECT unnest($sen::VARCHAR[]))")
        params.append(("sen", [s.lower() for s in crit.seniority]))
    if crit.location:
        where.append("(" + " OR ".join(f"regexp_matches(lower(location), $loc{i})" for i in range(len(crit.location))) + ")")
        params += [(f"loc{i}", place_pattern(v)) for i, v in enumerate(crit.location)]
    if crit.remote is True:
        where.append("remote = true")
    if crit.remote is False:
        where.append("(remote = false OR remote IS NULL)")
    if crit.min_years is not None:
        where.append("(years IS NULL OR years >= $miny)")
        params.append(("miny", float(crit.min_years)))
    if crit.rate_max is not None:
        where.append("(rate IS NULL OR rate <= $maxr)")
        params.append(("maxr", float(crit.rate_max)))
    sql = "SELECT doc_no, years IS NULL, rate IS NULL FROM profile WHERE " + " AND ".join(where)
    rows = index.con.execute(sql, dict(params)).fetchall()
    ok, unknown_years, unknown_rate = BitMap(), BitMap(), BitMap()
    for d, yn, rn in rows:
        y_blocked = crit.min_years is not None and yn
        r_blocked = crit.rate_max is not None and rn
        if not y_blocked and not r_blocked:
            ok.add(d)
        else:
            if y_blocked:
                unknown_years.add(d)
            if r_blocked:
                unknown_rate.add(d)
    if crit.availability:              # no stated availability = no match
        wanted = {availability_label(a) for a in crit.availability}
        rows = index.con.execute("SELECT doc_no, availability FROM profile WHERE availability IS NOT NULL").fetchall()
        match = BitMap(d for d, a in rows if availability_label(a) in wanted)
        ok, unknown_years, unknown_rate = ok & match, unknown_years & match, unknown_rate & match
    return ok, unknown_years, unknown_rate


def _ranks(scores: dict[int, float], docs: list[int]) -> dict[int, int]:
    """Competition ranks (1, 2, 2, 4) by descending score; docs without a score share the last rank."""
    ordered = sorted(docs, key=lambda d: -scores.get(d, float("-inf")))
    ranks: dict[int, int] = {}
    prev, rank = object(), 0
    for i, d in enumerate(ordered, start=1):
        v = scores.get(d, float("-inf"))
        if v != prev:
            rank, prev = i, v
        ranks[d] = rank
    return ranks


def rrf(docs: list[int], lists: list[dict[int, float]], k: int = RRF_K) -> dict[int, float]:
    rank_maps = [_ranks(s, docs) for s in lists if s]
    return {d: sum(1.0 / (k + rm[d]) for rm in rank_maps) for d in docs}


def rank(index: Index, membership_docs: BitMap, resolved: list[Resolved], text: TextResult | None, coverage: dict[int, int],
         like: LikeResult | None = None) -> list[int]:
    docs = list(membership_docs)
    if not resolved and text is None and like is None:
        return sorted(docs)
    ev: dict[int, float] = {}
    bm: dict[int, float] = {}
    cos: dict[int, float] = {}
    for r in resolved:
        for d, (e, b, c) in index.member_scores(r.slug).items():
            if d in membership_docs:
                ev[d] = max(ev.get(d, 0.0), float(e))
                bm[d] = max(bm.get(d, 0.0), b)
                cos[d] = max(cos.get(d, 0.0), c)
    lists = [ev, bm, cos]
    if text is not None:
        lists += [{d: s for d, s in text.bm25.items() if d in membership_docs}, {d: s for d, s in text.cosine.items() if d in membership_docs}]
    if like is not None:
        lists += [{d: s for d, s in like.cover.items() if d in membership_docs}, {d: s for d, s in like.cosine.items() if d in membership_docs},
                  {d: s for d, s in like.bm25.items() if d in membership_docs}]
    fused = rrf(docs, lists)
    return sorted(docs, key=lambda d: (-coverage.get(d, 1), -fused[d], d))


def collapse_versions(index: Index, order: list[int]) -> tuple[list[int], dict[int, int]]:
    """One version (the highest doc_no) per near-duplicate group → (order, doc → number of other versions)."""
    group_of = index.dup_group_of
    if not group_of:
        return order, {}
    members = set(order)
    keep: dict[int, int] = {}
    for d in order:
        g = group_of.get(d)
        if g is not None:
            keep[g] = max(keep.get(g, -1), d)
    versions = {d: len(index.dup_groups[g]) - 1 for g, d in keep.items() if len(index.dup_groups[g]) > 1}
    kept = [d for d in order if group_of.get(d) is None or keep[group_of[d]] == d]
    return kept, versions


def evaluate(index: Index, cfg: Config, crit: Criteria, *, universe: BitMap | None = None, parent_order: list[int] | None = None,
             collapse: bool = True) -> Membership:
    vocab = index.vocab
    resolved, unresolved = resolve_phrases(vocab, crit.topics, "topic")
    r2, u2 = resolve_phrases(vocab, crit.skills, "skill")
    seen = {r.slug for r in resolved}
    resolved += [r for r in r2 if r.slug not in seen]
    unresolved += u2
    text_parts = ([crit.text] if crit.text else []) + unresolved      # unresolved phrases fall back to text search
    text = search_text(index, cfg, " ".join(text_parts)) if text_parts else None

    like = None
    if crit.like:
        label, jd_text = read_source(index, crit.like, crit.like_session_dir)
        like = analyse(index, cfg, label, jd_text)

    per_term = [term_bitmaps(index, r, crit) for r in resolved]
    if text is not None:
        per_term.append((text.members, text.strong))
    base = index.all_docs if universe is None else (universe & index.all_docs)
    if per_term:                                   # explicit terms decide membership; --like only ranks
        members = combine([m for m, _ in per_term], crit.mode) & base
        strong = combine([s for _, s in per_term], crit.mode) & members
    elif like is not None:
        members = like.members & base
        strong = like.strong & members
    else:
        members = BitMap(base)
        strong = BitMap(base)

    unknown_years = unknown_rate = 0
    uy_bm, ur_bm = BitMap(), BitMap()
    cons = constraints(index, crit)
    if cons is not None:
        ok, uy, ur = cons
        uy_bm, ur_bm = members & uy, members & ur
        unknown_years, unknown_rate = len(uy_bm), len(ur_bm)
        blocked = (uy | ur) & members
        members = (members & ok) | (blocked if crit.include_unknown else BitMap())
        strong &= members

    coverage: dict[int, int] = {}
    if crit.mode != "all" and len(per_term) > 1:
        c: Counter[int] = Counter()
        for m, _ in per_term:
            c.update(m & members)
        coverage = dict(c)

    if parent_order is not None:
        order = [d for d in parent_order if d in members]
    else:
        order = rank(index, members, resolved, text, coverage, like)
    versions: dict[int, int] = {}
    if collapse:
        order, versions = collapse_versions(index, order)
    members = BitMap(order)
    strong &= members
    return Membership(resolved, unresolved, text, members, strong, order, coverage, unknown_years, unknown_rate, versions, like, uy_bm, ur_bm)
