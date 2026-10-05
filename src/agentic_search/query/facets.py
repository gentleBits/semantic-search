"""Facets over a member list: a few aggregates in DuckDB."""

from __future__ import annotations

from pyroaring import BitMap

from ..index.cards import _availability
from .index import Index, _in_list

SENIORITY_RANK = {"intern": 0, "junior": 1, "mid": 2, "senior": 3, "lead": 4, "principal": 5, "manager": 5, "director": 6, "executive": 7}
TOP_SKILLS = 8
TOP_PLACES = 5
TOP_COUNTRIES = 40      # the last comma-separated part of a location; 40 covers all of a set's in practice


def seniority_rank(s: str | None) -> int | None:
    return SENIORITY_RANK.get(s.lower()) if s else None


def _q(v) -> int | None:
    return None if v is None else int(round(v))


def compute(index: Index, members: BitMap | list[int], strong: BitMap | None = None) -> dict:
    docs = list(members)
    n = len(docs)
    out: dict = {"count": n}
    if strong is not None:
        out["strong"], out["weak"] = len(strong), n - len(strong)
    if n == 0:
        return out
    con = index.con
    row = con.execute(
        "SELECT quantile_cont(years, [0.25, 0.5, 0.75]), count(*) FILTER (WHERE years IS NULL), "
        "quantile_cont(rate, [0.25, 0.5, 0.75]), count(*) FILTER (WHERE rate_source = 'synthetic'), any_value(currency), "
        "count(*) FILTER (WHERE remote = true) "
        f"FROM profile WHERE doc_no {_in_list(docs)}").fetchone()
    years, rate = list(row[0] or [None] * 3), list(row[2] or [None] * 3)
    out["years"] = {"p25": _q(years[0]), "p50": _q(years[1]), "p75": _q(years[2]), "unknown": int(row[1])}
    out["rate"] = {"p25": _q(rate[0]), "p50": _q(rate[1]), "p75": _q(rate[2]), "synthetic": int(row[3]), "currency": row[4] or "EUR"}
    out["seniority"] = sorted(
        ((s, int(c)) for s, c in con.execute(f"SELECT lower(seniority), count(*) FROM profile WHERE doc_no {_in_list(docs)} AND seniority IS NOT NULL GROUP BY 1").fetchall()),
        key=lambda x: (SENIORITY_RANK.get(x[0], 9), -x[1]),
    )
    out["skills"] = [
        (name, int(c)) for name, c in con.execute(
            "SELECT t.canonical, count(*) c FROM doc_terms dt JOIN terms t USING (term_id) "
            f"WHERE dt.doc_no {_in_list(docs)} AND t.kind = 'skill' AND dt.via <> 'implied' GROUP BY 1 ORDER BY c DESC, 1 LIMIT {TOP_SKILLS}").fetchall()
    ]
    places = con.execute(
        f"SELECT trim(split_part(location, ',', 1)) p, count(*) c FROM profile WHERE doc_no {_in_list(docs)} AND location IS NOT NULL GROUP BY 1 ORDER BY c DESC, 1 LIMIT {TOP_PLACES}").fetchall()
    countries = con.execute(
        f"SELECT trim(split_part(location, ',', -1)) p, count(*) c FROM profile WHERE doc_no {_in_list(docs)} AND location IS NOT NULL GROUP BY 1 ORDER BY c DESC, 1 LIMIT {TOP_COUNTRIES}").fetchall()
    stated = con.execute(f"SELECT count(*) FROM profile WHERE doc_no {_in_list(docs)} AND location IS NOT NULL").fetchone()[0]
    out["where"] = {"remote_ok": int(row[5]), "stated": int(stated), "top": [(p, int(c)) for p, c in places], "countries": [(p, int(c)) for p, c in countries if p]}
    avail: dict[str, int] = {}
    for a, c in con.execute(f"SELECT availability, count(*) FROM profile WHERE doc_no {_in_list(docs)} AND availability IS NOT NULL GROUP BY 1").fetchall():
        label = (_availability(a) or "other").removeprefix("avail ")
        label = label if label in ("now", "2w", "1m", "3m") or label[:-1].isdigit() else "other"
        avail[label] = avail.get(label, 0) + int(c)
    order = {"now": 0, "1w": 1, "2w": 2, "1m": 3, "3m": 4}
    out["availability"] = sorted(avail.items(), key=lambda x: (order.get(x[0], 8), x[0]))
    return out
