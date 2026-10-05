"""When a set is too big to rank: the filters that would bring it under the limit.

Each count is computed the way `filter` computes it, so a suggestion never promises a number the
next call does not deliver.
"""

from __future__ import annotations

import math

from pyroaring import BitMap

from ..session.bitmap import decode  # noqa: F401
from ..session.filters import Filter
from .facets import SENIORITY_RANK
from .index import Index, _in_list
from .search import place_matches, availability_label

CANDIDATE_SKILLS = 80
MIN_PEOPLE = 2
SHOWN = {"skill": 4, "seniority": 3, "where": 3, "avail": 2}
AVAIL_ORDER = {"now": 0, "1w": 1, "2w": 2, "1m": 3, "3m": 4}


def _person(index: Index):
    group_of = index.dup_group_of
    return lambda d: ("g", group_of[d]) if d in group_of else ("d", d)


def suggestions(index: Index, raw: BitMap, active: list[Filter], limit: int) -> list[dict]:
    docs = list(raw)
    if not docs:
        return []
    person = _person(index)
    total = len({person(d) for d in docs})
    ok = lambda n: MIN_PEOPLE <= n <= limit and n < total      # noqa: E731
    out: list[dict] = []

    # counted on the same bitmap `filter --skill` uses
    taken = {s for f in active for s in f.resolution.get("resolved", [])}
    con = index.con
    cand = [r[0] for r in con.execute(
        "SELECT t.slug, count(*) c FROM doc_terms dt JOIN terms t USING (term_id) "
        f"WHERE dt.doc_no {_in_list(docs)} AND t.kind = 'skill' GROUP BY 1 ORDER BY c DESC, 1 LIMIT {CANDIDATE_SKILLS}").fetchall() if r[0] not in taken]
    skills = []
    if cand:
        wanted = set(cand)
        rows = [r for r in con.execute("SELECT t.slug, p.strong_bm FROM postings p JOIN terms t USING (term_id) WHERE t.kind = 'skill'").fetchall() if r[0] in wanted]
        for slug, blob in rows:
            n = len({person(d) for d in (BitMap.deserialize(bytes(blob)) & raw)})
            if ok(n):
                skills.append((n, slug))
    for n, slug in sorted(skills, key=lambda x: (-x[0], x[1]))[: SHOWN["skill"]]:
        out.append({"group": "skill", "flag": "--skill", "value": slug, "count": n})

    rows = con.execute(f"SELECT doc_no, lower(seniority), location, remote, rate, years, availability FROM profile WHERE doc_no {_in_list(docs)}").fetchall()
    by_sen: dict[str, set] = {}
    by_avail: dict[str, set] = {}
    cities: dict[str, int] = {}
    remote: set = set()
    rates: list[tuple[float, tuple]] = []
    years: list[tuple[float, tuple]] = []
    locs: list[tuple[str, tuple]] = []
    for d, sen, loc, rem, rate, yrs, avail in rows:
        p = person(d)
        if avail and availability_label(avail) in AVAIL_ORDER:
            by_avail.setdefault(availability_label(avail), set()).add(p)
        if sen:
            by_sen.setdefault(sen, set()).add(p)
        if loc:
            locs.append((loc.lower(), p))
            city = loc.split(",", 1)[0].strip()
            if city:
                cities[city] = cities.get(city, 0) + 1
        if rem:
            remote.add(p)
        if rate is not None:
            rates.append((float(rate), p))
        if yrs is not None:
            years.append((float(yrs), p))

    sen = [(len(ps), s) for s, ps in by_sen.items() if ok(len(ps))]
    for n, s in sorted(sen, key=lambda x: (-x[0], SENIORITY_RANK.get(x[1], 9)))[: SHOWN["seniority"]]:
        out.append({"group": "seniority", "flag": "--seniority", "value": s, "count": n})

    where = []
    for city in sorted(cities, key=lambda c: (-cities[c], c))[:12]:
        n = len({p for loc, p in locs if place_matches(city, loc)})      # as `--location` matches
        if ok(n):
            where.append((n, city))
    for n, city in sorted(where, key=lambda x: (-x[0], x[1]))[: SHOWN["where"]]:
        out.append({"group": "where", "flag": "--location", "value": city, "count": n})

    if ok(len(remote)):
        out.append({"group": "remote", "flag": "--remote", "value": None, "count": len(remote)})

    av = [(len(ps), a) for a, ps in by_avail.items() if ok(len(ps))]
    for n, a in sorted(av, key=lambda x: (AVAIL_ORDER[x[1]], -x[0]))[: SHOWN["avail"]]:
        out.append({"group": "avail", "flag": "--availability", "value": a, "count": n})

    # the highest rate limit / the lowest years floor that still leaves ≤ limit people
    if rates:
        best = None
        for t in sorted({math.floor(r) for r, _ in rates}):
            n = len({p for r, p in rates if r <= t})
            if n > limit:
                break
            if ok(n):
                best = (t, n)
        if best:
            out.append({"group": "rate", "flag": "--rate-max", "value": best[0], "count": best[1]})
    if years:
        best = None
        for t in sorted({math.ceil(y) for y, _ in years}, reverse=True):
            n = len({p for y, p in years if y >= t})
            if n > limit:
                break
            if ok(n):
                best = (t, n)
        if best:
            out.append({"group": "years", "flag": "--min-years", "value": best[0], "count": best[1]})
    return out


def call_of(s: dict) -> str:
    return f"resumes filter {s['flag']}" + ("" if s["value"] is None else f" {s['value']}")


def line(sugg: list[dict], limit: int, currency_symbol: str = "€") -> str:
    """`narrow to ≤ 50 with:  skill kubernetes 47 · redis 47 | seniority lead 15 | where Madrid 8 | remote 41 | rate ≤ €52 48 | years ≥ 20 44`."""
    if not sugg:
        return f"no single filter brings it to ≤ {limit}: combine filters (`resumes filter --skill … --seniority …`)"
    groups: dict[str, list[str]] = {}
    for s in sugg:
        g = s["group"]
        if g == "remote":
            groups.setdefault(g, []).append(f"remote {s['count']}")
        elif g == "rate":
            groups.setdefault(g, []).append(f"rate ≤ {currency_symbol}{s['value']} {s['count']}")
        elif g == "years":
            groups.setdefault(g, []).append(f"years ≥ {s['value']} {s['count']}")
        else:
            groups.setdefault(g, []).append(f"{s['value']} {s['count']}")
    parts = []
    for g, items in groups.items():
        parts.append(" · ".join(items) if g in ("remote", "rate", "years") else f"{g} " + " · ".join(items))
    return f"narrow to ≤ {limit} with:  " + " | ".join(parts)
