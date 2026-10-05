"""The snapshot the right pane renders: one JSON object, rebuilt after every step.

Read-only, except the cursor: the list is always shown, so a set nobody paged yet is put on page 1.
"""

from __future__ import annotations

import re
from pathlib import Path

from pyroaring import BitMap

from ..errors import ResumesError
from ..index.cards import _title_case
from ..query import facets as facets_mod
from ..query import narrow, render, verbs, view
from ..query.index import _in_list
from ..query.search import availability_label
from ..query.verbs import Context
from ..session import judgments as jmod
from ..session.filters import Filter
from ..session.store import ResultSet, Session, set_number
from .store import WebSession

EXPANSION_SHOWN = 4
SUGGESTIONS_SHOWN = 8
SKILLS_ROW = 5
SORT_NAME = {"judgment": "Ranking", "rate": "Rate", "years": "Years", "seniority": "Seniority", "relevance": "Relevance"}
AVAIL_UNIT = {"d": "d", "w": "wk", "m": "mo"}
GROUP_ORDER = ["skill", "seniority", "avail", "rate", "remote", "where", "years"]
SECONDS_PER_ROUND = 7.0        # a first guess, until a ranking job has measured one round


# ---------------------------------------------------------------- small formatters


def fmt(n: int) -> str:
    return f"{n:,}"


def people(ctx: Context, n: int) -> str:
    w = ctx.cfg.web
    return f"{fmt(n)} {w.noun if n == 1 else w.nouns}"


def chip_text(v: dict) -> str:
    return f"{v['kind']}: {v['value']}" if v.get("kind") else str(v.get("value"))


def estimate(cfg, n: int, per_round: float | None = None) -> float:
    """Seconds a ranking of `n` people takes, at the pace the last job measured."""
    rounds = -(-n // (cfg.web.judge_batch * cfg.web.judge_parallel))
    return max(3.0, rounds * (per_round or SECONDS_PER_ROUND))


def screen_line(ctx: Context, snap: dict) -> str:
    """What the screen shows, in one line for the model. The ranking limit is not named: a model that knows the
    number skips the rank call that keeps the criterion pending."""
    s, r = snap["set"], snap["ranking"]
    chips = ", ".join(f"{f['id']} {chip_text(f)}" for f in snap["filters"]) or "none (the whole collection)"
    bits = [people(ctx, s["count"]), f"page {s['page']} of {s['pages']}", f"filters: {chips}", f"order: {snap['sort']['label']}"]
    if r:
        bits.append(f"ranking “{r['criterion']}”: {r['judged']} of {s['count']} ranked" + ("" if r["sorted"] else " (list not ordered by it)"))
    elif snap["ranking_off"]:
        bits.append("ranking: turned off")
    else:
        bits.append("not ranked")
    pending = snap["rank"]["pending"]
    if pending:
        bits.append(f"pending ranking “{pending}” — " + ("waits for a smaller set" if s["count"] > snap["rank"]["limit"] else "the set is small enough now: call rank"))
    return "[screen] " + " · ".join(bits)


def avail(raw: str | None) -> tuple[str | None, str | None]:
    """`immediately` → (now, now) · `2 weeks` → (2w, 2 wk) · anything else → (other, the text)."""
    if not raw:
        return None, None
    code = availability_label(raw)
    if code == "now":
        return "now", "now"
    m = re.fullmatch(r"(\d+)([dwm])", code)
    if m:
        return code, f"{m.group(1)} {AVAIL_UNIT[m.group(2)]}"
    return "other", raw.strip()[:24]


def avail_label(code: str) -> str:
    m = re.fullmatch(r"(\d+)([dwm])", code)
    return f"{m.group(1)} {AVAIL_UNIT[m.group(2)]}" if m else code


def years_label(y: float | None) -> str:
    return "—" if y is None else render.fmt_years(y)


def rate_label(rate: float | None, currency: str | None, source: str | None) -> str:
    if rate is None:
        return "—"
    return ("~" if source == "synthetic" else "") + f"{render.rate_symbol(currency)}{rate:.0f}"


def city(location: str | None) -> str | None:
    return location.split(",", 1)[0].strip() or None if location else None


def sort_view(rs: ResultSet, ranked_base: bool, like: bool = False) -> dict:
    keys = [{"key": k, "dir": d} for k, d in view.parse_sort(rs.sort)]
    base = ("match" if like else "relevance") if ranked_base else "newest"
    names = {"match": "Best match", "relevance": "Relevance", "newest": "Newest first"}
    if keys:
        label = " · ".join(f"{SORT_NAME.get(k['key'], k['key'])} {'↓' if k['dir'] == 'desc' else '↑'}" for k in keys)
    else:
        label = names[base]
    return {"keys": keys, "label": label, "base": base, "base_label": names[base]}


# ---------------------------------------------------------------- filters → chips


def shown_name(kind: str, name: str) -> str:
    """Topics are stored in lower case (`data pipelines`), skills as written (`dbt`, `Elixir`)."""
    return name[:1].upper() + name[1:] if kind == "topic" else name


def _names(ctx: Context, slugs: list[str]) -> list[str]:
    by = ctx.index.vocab.by_slug
    return [shown_name(by[s].kind, by[s].canonical) if s in by else s for s in slugs]


def filter_view(ctx: Context, web: WebSession | None, f: Filter) -> dict:
    """`kind` + `value` go on the chip, the rest in its popover."""
    a = f.args or {}
    sym = render.rate_symbol(ctx.cfg.currency)
    out: dict = {"id": f.id, "kind": "", "value": f.label, "said": f.said, "args": a, "terms": [], "unresolved": [], "ranked": f.ranked}
    if f.kind == "frozen":
        out.update(kind="set", value=f.label)
        return out
    if a.get("min_years") is not None:
        out.update(kind="years", value=f"≥ {a['min_years']:g}")
    elif a.get("rate_max") is not None:
        out.update(kind="rate", value=f"≤ {sym}{a['rate_max']:g}/h")
    elif a.get("seniority"):
        out.update(kind="seniority", value=", ".join(a["seniority"]))
    elif a.get("availability"):
        out.update(kind="available", value=", ".join(avail_label(x) for x in a["availability"]))
    elif a.get("location"):
        loc = a["location"]
        out.update(kind="location", value=" or ".join(loc) if isinstance(loc, list) else str(loc))
    elif a.get("remote") is not None and not (a.get("topic") or a.get("skill") or a.get("text") or a.get("like")):
        out.update(kind="", value="remote" if a["remote"] else "not remote")
    else:
        resolved = (f.echo or {}).get("resolved") or []
        expanded = (f.resolution or {}).get("expanded") or {}
        terms = []
        for r in resolved:
            exp = _names(ctx, expanded.get(r["slug"], []))
            terms.append({"kind": r["kind"], "name": shown_name(r["kind"], r["canonical"]), "slug": r["slug"], "given": r["given"], "how": r["how"],
                          "expanded": exp[:EXPANSION_SHOWN], "more": max(0, len(exp) - EXPANSION_SHOWN)})
        mode = a.get("mode", "all")
        names = [t["name"] for t in terms]
        joined = (" + " if mode == "all" else " or ").join(names) if isinstance(mode, str) else f"≥ {mode} of " + ", ".join(names)
        kinds = {t["kind"] for t in terms}
        kind = (next(iter(kinds)) + ("s" if len(terms) > 1 else "")) if len(kinds) == 1 else ("terms" if terms else "")
        text = a.get("text") or ((f.echo or {}).get("text") or {}).get("query")      # words no term matched
        like = a.get("like")
        if like and re.fullmatch(r"r\d{6}", str(like)):            # a resume id stands in for the description
            try:
                like = _title_case(ctx.index.doc(like).title or like)
            except ResumesError:
                pass
        elif like and web is not None:
            like = web.jd_name(like)
        elif like:
            like = Path(like).name
        if terms:
            value = joined + (f" · “{text}”" if text else "") + (f" · like {like}" if like else "")
        elif text:
            kind, value = "text", f"“{text}”"
        elif like:
            kind, value = "like", str(like)
        else:
            kind, value = "", f.label
        if a.get("level") == "used":
            value += " · used in a job"
        if a.get("strict") is True:
            value += " · exact only"
        out.update(kind=kind, value=value, terms=terms, unresolved=list((f.echo or {}).get("unresolved") or []))
        if (f.echo or {}).get("like"):
            lk = f.echo["like"]
            out["like"] = {"source": like or lk.get("source"), "requirements": lk.get("requirements"), "resolved": _names(ctx, lk.get("resolved") or []),
                           "unresolved": len(lk.get("unresolved") or [])}
    return out


def filters_view(ctx: Context, web: WebSession | None, session: Session, rs: ResultSet, active: list[Filter]) -> list[dict]:
    index = ctx.index
    members = rs.bitmap()
    synthetic = None
    out = []
    for i, f in enumerate(active):
        v = filter_view(ctx, web, f)
        others = view.raw_members(index, [g for g in active if g.id != f.id])
        v["alone"] = view.people(index, f.members)
        v["without"] = view.people(index, others)
        v["culprit"] = rs.count == 0 and i == len(active) - 1
        if f.ranked and f.kind != "frozen":
            v["by_meaning"] = len(members - f.strong)
        a = f.args or {}
        if a.get("min_years") is not None or a.get("rate_max") is not None:
            unknown = f.unknown_years if a.get("min_years") is not None else f.unknown_rate
            v["unknown"] = {"count": view.people(index, others & unknown), "included": bool(a.get("include_unknown")),
                            "what": "years" if a.get("min_years") is not None else "rate"}
        if a.get("rate_max") is not None and rs.count:
            if synthetic is None:
                synthetic = int(index.con.execute(f"SELECT count(*) FROM profile WHERE rate_source = 'synthetic' AND doc_no {_in_list(rs.order)}").fetchone()[0])
            v["estimated"] = synthetic
        out.append(v)
    return out


# ---------------------------------------------------------------- suggestions (too many to rank)


def suggestion_view(ctx: Context, s: dict) -> dict:
    g, v = s["group"], s["value"]
    sym = render.rate_symbol(ctx.cfg.currency)
    if g == "skill":
        name = _names(ctx, [v])[0]
        return {"kind": "skill", "value": name, "count": s["count"], "args": {"skill": [name]}}
    if g == "seniority":
        return {"kind": "seniority", "value": v, "count": s["count"], "args": {"seniority": [v]}}
    if g == "where":
        return {"kind": "location", "value": v, "count": s["count"], "args": {"location": v}}
    if g == "remote":
        return {"kind": "", "value": "remote", "count": s["count"], "args": {"remote": True}}
    if g == "avail":
        return {"kind": "available", "value": avail_label(v), "count": s["count"], "args": {"availability": [v]}}
    if g == "rate":
        return {"kind": "rate", "value": f"≤ {sym}{v}", "count": s["count"], "args": {"rate_max": v}}
    return {"kind": "years", "value": f"≥ {v}", "count": s["count"], "args": {"min_years": v}}


def suggestions(ctx: Context, active: list[Filter], shown: int = SUGGESTIONS_SHOWN) -> list[dict]:
    """Filters that bring the set under the ranking limit, interleaved by kind so the first few differ."""
    raw = narrow.suggestions(ctx.index, view.raw_members(ctx.index, active), active, ctx.cfg.query.max_cards)
    groups: dict[str, list[dict]] = {}
    for s in raw:
        groups.setdefault(s["group"], []).append(s)
    picked: list[dict] = []
    depth = 0
    while len(picked) < shown and any(len(groups.get(g, [])) > depth for g in GROUP_ORDER):
        for g in GROUP_ORDER:
            if len(groups.get(g, [])) > depth and len(picked) < shown:
                picked.append(groups[g][depth])
        depth += 1
    return [suggestion_view(ctx, s) for s in picked]


# ---------------------------------------------------------------- rows


def parse_card(card: str | None) -> tuple[list[str], list[str], str | None]:
    """(skills used in jobs, skills listed only, the `did` line) of a stored card."""
    used: list[str] = []
    listed: list[str] = []
    did = None
    for line in (card or "").split("\n"):
        if line.startswith("skills: "):
            body = line[len("skills: "):]
            head, sep, tail = body.partition("listed only: ")
            used = [s.strip() for s in head.rstrip(" ·").split(", ") if s.strip()]
            listed = [s.strip() for s in tail.split(", ") if s.strip()] if sep else []
        elif line.startswith("did: "):
            did = line[len("did: "):].strip()
    return used, listed, did


def rows(ctx: Context, session: Session, rs: ResultSet, start: int, end: int, *, scores: dict, strong: BitMap | None, job: dict | None = None) -> list[dict]:
    docs = rs.order[start:end]
    profiles, cards, versions = ctx.index.profiles(docs), ctx.index.cards(docs), verbs._versions_of(ctx, docs)
    flying = (job or {}).get("scores") or {}
    waiting = (job or {}).get("waiting") or set()
    out = []
    for i, d in enumerate(docs, start=start + 1):
        p = profiles.get(d)
        if p is None or p.deleted:
            out.append({"rank": i, "doc_no": d, "id": None, "title": "removed from the index", "removed": True})
            continue
        used, listed, did = parse_card(cards.get(d))
        judged = scores.get(d) if scores.get(d, (None, ""))[0] is not None else None
        if judged is None and d in flying:
            judged = flying[d]
        code, label = avail(p.availability)
        out.append({
            "rank": i, "id": p.id, "doc_no": d, "title": _title_case(p.title or "Resume"), "seniority": (p.seniority or "").lower() or None,
            "years": p.years, "years_label": years_label(p.years),
            "rate": p.rate, "rate_label": rate_label(p.rate, p.currency, p.rate_source), "rate_estimated": p.rate_source == "synthetic", "currency": p.currency,
            "location": city(p.location), "location_full": p.location, "remote": bool(p.remote), "avail": code, "avail_label": label,
            "versions": versions.get(d, 0), "used": used, "listed": listed, "skills": " · ".join((used or listed)[:SKILLS_ROW]), "did": did,
            "score": judged[0] if judged else None, "note": (judged[1] or None) if judged else None,
            "pending": judged is None and d in waiting,
            "by_meaning": strong is not None and d not in strong,
            "link": ctx.index.link(p),
        })
    return out


# ---------------------------------------------------------------- overview


def overview(ctx: Context, rs: ResultSet, strong: BitMap | None) -> dict:
    return overview_of(ctx, rs.order, strong)


def overview_of(ctx: Context, docs: list[int] | BitMap, strong: BitMap | None = None) -> dict:
    members = BitMap(docs)
    f = facets_mod.compute(ctx.index, members, (strong & members) if strong is not None else None)
    out: dict = {"count": f["count"], "direct": f.get("strong"), "by_meaning": f.get("weak")}
    if not f["count"]:
        return out
    lo_hi = ctx.index.con.execute(f"SELECT min(years), max(years), min(rate), max(rate) FROM profile WHERE doc_no {_in_list(list(members))}").fetchone()
    out["seniority"] = [{"name": s, "count": n} for s, n in f.get("seniority", [])]
    out["years"] = {**f["years"], "min": lo_hi[0], "max": lo_hi[1]}
    r = f["rate"]
    out["rate"] = {"p25": r["p25"], "p50": r["p50"], "p75": r["p75"], "estimated": r["synthetic"], "currency": r["currency"],
                   "symbol": render.rate_symbol(r["currency"]), "min": lo_hi[2], "max": lo_hi[3]}
    out["skills"] = [{"name": s, "count": n} for s, n in f.get("skills", [])]
    out["remote_ok"] = f["where"]["remote_ok"]
    out["location_stated"] = f["where"]["stated"]
    out["cities"] = [{"name": p, "count": n} for p, n in f["where"]["top"] if p]
    out["countries"] = [{"name": p, "count": n} for p, n in f["where"]["countries"]]
    out["availability"] = [{"code": a, "label": avail_label(a), "count": n} for a, n in f.get("availability", [])]
    return out


# ---------------------------------------------------------------- history


def _step_label(ctx: Context, web: WebSession | None, session: Session, rs: ResultSet, by_id: dict[str, ResultSet], chips: dict[str, dict]) -> tuple[str, str]:
    """(sign, words) of one step of the trail: `+ skill: Elixir`, `− remote`, `★ ranked`."""

    def chip(fid: str) -> str:
        if fid not in chips:
            try:
                from ..session import filters as fmod

                chips[fid] = filter_view(ctx, web, fmod.load(session, fid))
            except ResumesError:
                chips[fid] = {"kind": "", "value": fid}
        c = chips[fid]
        return f"{c['kind']}: {c['value']}" if c["kind"] else c["value"]

    if rs.op == "all":
        return "", "start · everyone"
    if rs.op == "clear":
        return "−", "all filters · everyone"
    if rs.op in ("search", "filter"):
        before = set(by_id[rs.parent].filters or []) if rs.parent in by_id and rs.op == "filter" else set()
        new = [f for f in (rs.filters or []) if f not in before]
        words = ", ".join(chip(f) for f in new) or "same filters"
        return ("?" if rs.op == "search" else "+"), words
    if rs.op == "drop":
        removed = []
        for label in rs.args.get("removed", []):
            fid = label.split(" ", 1)[0]
            removed.append("ranking" if label == "the ranking" else chip(fid) if re.fullmatch(r"f\d+", fid) else label)
        return "−", ", ".join(removed)
    if rs.op == "sort":
        if any(k.startswith("judgment") for k in rs.sort):
            crit = jmod.criterion_of(session, rs.judgment, ctx.index.con) if rs.judgment else None
            return "★", "ranked" + (f" · {crit}" if crit else "")
        return "↕", "sorted · " + sort_view(rs, True)["label"]
    if rs.op in ("union", "minus"):
        return "", f"{rs.parent} {'∪' if rs.op == 'union' else '−'} {rs.args.get('with')}"
    return "", rs.op


def history(ctx: Context, web: WebSession | None, session: Session, current: str) -> list[dict]:
    sets = session.all_sets()
    by_id = {r.id: r for r in sets}
    chips: dict[str, dict] = {}
    out = []
    for n, r in enumerate(sets, start=1):
        sign, words = _step_label(ctx, web, session, r, by_id, chips)
        out.append({"id": r.id, "n": n, "parent": r.parent, "op": r.op, "sign": sign, "label": words, "count": r.count,
                    "ranked": any(k.startswith("judgment") for k in r.sort), "current": r.id == current})
    return out


# ---------------------------------------------------------------- the snapshot


def rankings(ctx: Context, session: Session, rs: ResultSet) -> list[dict]:
    """Every judgment of the conversation, with how many of this set it covers."""
    by: dict[str, dict] = {}
    for j in jmod.list_judgments(session, ctx.index.con):
        by.setdefault(j["id"], {"judgment": j["id"], "criterion": j["criterion"]})
    for jid, row in by.items():
        row["covers"] = verbs._n_judged(rs, ctx.scores(session, jid))
    return sorted(by.values(), key=lambda r: set_number(r["judgment"]))


def snapshot(ctx: Context, web: WebSession | None = None, *, job: dict | None = None, with_overview: bool = True, per_round: float | None = None) -> dict:
    """`job`: the running ranking job's `public()` view."""
    session, rs = verbs._current(ctx)
    if rs.cursor == 0 and rs.count:
        session.save_cursor(rs, min(rs.page_size, rs.count))
    page = max(1, rs.page_no) if rs.count else 0
    start = (page - 1) * rs.page_size if rs.count else 0
    end = min(start + rs.page_size, rs.count)

    active = view.filters_of(ctx.index, ctx.cfg, session, rs)
    anchor = view.anchor_of(active)
    strong = anchor.strong if anchor is not None and anchor.kind != "frozen" else None
    jid, scores = verbs._judgment_for(ctx, session, rs)
    judged = verbs._n_judged(rs, scores) if jid else 0
    limit = ctx.cfg.query.max_cards
    all_rankings = rankings(ctx, session, rs)
    running = job if job and job.get("running") else None

    ranking = None
    if jid:
        ranking = {"judgment": jid, "criterion": jmod.criterion_of(session, jid, ctx.index.con) or "", "judged": judged, "total": rs.count,
                   "sorted": any(k.startswith("judgment") for k in rs.sort)}
    pending = (web.pending or {}).get("criterion") if web is not None else None
    too_many = None
    if rs.count > limit and pending:
        too_many = {"count": rs.count, "limit": limit, "pending": pending, "suggestions": suggestions(ctx, active)}

    out = {
        "session": {"id": session.id, "title": web.title if web is not None else None},
        "set": {"id": rs.id, "count": rs.count, "page": page, "pages": rs.pages, "page_size": rs.page_size, "start": start + 1 if rs.count else 0,
                "end": end, "parent": rs.parent, "prev": (web.prev_of(rs.id) if web is not None else None) or rs.parent, "op": rs.op,
                "everyone": not active, "delta": web.delta_of(rs.id) if web is not None else None,
                "index_changed": rs.index_version != ctx.index.version},
        "filters": filters_view(ctx, web, session, rs, active),
        "sort": sort_view(rs, anchor is not None, bool(anchor is not None and (anchor.args or {}).get("like"))),
        "ranking": ranking,
        "ranking_off": bool(rs.ranking_off) and jid is None,
        "rankings": all_rankings,
        "rank": {"limit": limit, "possible": 0 < rs.count <= limit, "unranked": (rs.count - judged) if jid else rs.count, "pending": pending,
                 "estimate": round(estimate(ctx.cfg, (rs.count - judged) if jid else rs.count, per_round), 1),
                 "running": ({"criterion": running["criterion"], "done": running["done"], "total": running["total"], "seconds": running["seconds"],
                              "set": running["set"]} if running else None)},
        "too_many": too_many,
        "items": rows(ctx, session, rs, start, end, scores=scores, strong=strong, job=running if running and running.get("set") == rs.id else None),
        "history": history(ctx, web, session, rs.id),
    }
    if with_overview:
        out["overview"] = overview(ctx, rs, strong)
    if rs.count == 0 and active:
        opts = sorted(out["filters"], key=lambda f: (not f["culprit"], -f["without"]))
        out["zero"] = {"filters": len(active), "culprit": next((f["id"] for f in out["filters"] if f["culprit"]), None),
                       "options": [{"id": f["id"], "kind": f["kind"], "value": f["value"], "without": f["without"]} for f in opts if f["without"] > 0]}
    out["screen"] = screen_line(ctx, out)
    return out


# ---------------------------------------------------------------- one person


def strip_front_matter(md: str) -> str:
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            return md[end + 4:].lstrip("\n")
    return md


def person(ctx: Context, web: WebSession | None, ref: str) -> dict:
    """One resume in detail, with its neighbours in the set."""
    session, rs = verbs._current(ctx)
    ref = ref.strip()
    m = re.fullmatch(r"#?(\d{1,5})", ref)
    if m and not re.fullmatch(r"r\d{6}", ref):
        n = int(m.group(1))
        if n < 1 or n > rs.count:
            raise ResumesError("NO_SUCH_RANK", f"#{n} (the set has {rs.count})")
        p = ctx.index.profiles([rs.order[n - 1]])[rs.order[n - 1]]
    else:
        p = ctx.index.doc(ref)
    pos = rs.order.index(p.doc_no) if p.doc_no in set(rs.order) else None
    active = view.filters_of(ctx.index, ctx.cfg, session, rs)
    anchor = view.anchor_of(active)
    jid, scores = verbs._judgment_for(ctx, session, rs)
    item = rows(ctx, session, ResultSet(id=rs.id, parent=None, op="", args={}, index_version=rs.index_version, count=1, order=[p.doc_no]), 0, 1,
                scores=scores, strong=anchor.strong if anchor is not None and anchor.kind != "frozen" else None)[0]
    item["rank"] = pos + 1 if pos is not None else None
    skills = ctx.index.con.execute(
        "SELECT t.canonical, dt.level FROM doc_terms dt JOIN terms t USING (term_id) WHERE dt.doc_no = ? AND t.kind = 'skill' AND dt.via <> 'implied' "
        "ORDER BY CASE dt.level WHEN 'led' THEN 0 WHEN 'used' THEN 1 ELSE 2 END, t.canonical", [p.doc_no]).fetchall()
    used_card = item["used"]
    rest_used = [s for s, lvl in skills if lvl in ("led", "used") and s not in used_card]
    listed_card = item["listed"]
    rest_listed = [s for s, lvl in skills if lvl == "listed" and s not in listed_card]
    extra = ctx.index.con.execute("SELECT did, summary, education FROM profile WHERE doc_no = ?", [p.doc_no]).fetchone() or (None, None, None)
    ids = ctx.index.ids([rs.order[pos - 1]] if pos else []) if pos else {}
    nxt = ctx.index.ids([rs.order[pos + 1]]) if pos is not None and pos + 1 < rs.count else {}
    better = None
    if jid and item["score"] is not None:
        if pos is not None and any(k.startswith("judgment") for k in rs.sort):
            better = pos + 1                     # the list is sorted by the ranking
        else:
            better = 1 + sum(1 for d in rs.order if scores.get(d, (None, ""))[0] is not None and scores[d][0] > item["score"])
    return {
        **item,
        "used": used_card + rest_used, "listed": listed_card + rest_listed,
        "did": item["did"] or extra[0], "education": extra[2],
        "markdown": strip_front_matter(ctx.index.markdown(p.doc_no) or ""),
        "in_set": pos is not None, "of": rs.count, "set": rs.id,
        "prev": next(iter(ids.values()), None), "next": next(iter(nxt.values()), None),
        "criterion": jmod.criterion_of(session, jid, ctx.index.con) if jid and item["score"] is not None else None,
        "place": better, "judged": verbs._n_judged(rs, scores) if jid else 0,
    }
