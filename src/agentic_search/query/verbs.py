"""The verbs: one function per command, returning text for the agent and data for --json.

`cli.py` and `mcp.py` are thin adapters over these. Verbs read the index and write only to the session directory.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..errors import ResumesError
from ..session import filters as fmod
from ..session import judgments as jmod
from ..session import store
from ..session.filters import Filter
from ..session.store import ResultSet, Session
from . import facets as facets_mod
from . import narrow, render, view
from .index import Index, Profile
from .search import Criteria, Membership, describe_args
from .sort import parse_keys


@dataclass
class Output:
    text: str
    data: dict = field(default_factory=dict)


class Context:
    def __init__(self, cfg: Config, *, session_id: str | None = None, tty: bool = False) -> None:
        self.cfg = cfg
        self.session_id = session_id
        self.tty = tty
        self._index: Index | None = None
        self._session: Session | None = None
        self._scores: dict[tuple[str, str], dict[int, tuple[int | None, str]]] = {}
        self._jid: dict[tuple[str, str], str | None] = {}

    @property
    def index(self) -> Index:
        if self._index is None:
            self._index = Index(self.cfg)
        return self._index

    @property
    def sessions_dir(self) -> Path:
        return self.cfg.query.sessions

    def session(self, *, create: bool = False) -> Session:
        if self._session is None:
            s = store.open_session(self.sessions_dir, self.index.version, create=create, explicit=self.session_id)
            if s is None:
                raise ResumesError("NO_SESSION", "no result sets yet: run `resumes search …` first")
            self._session = s
        return self._session

    def scores(self, session: Session, jid: str) -> dict[int, tuple[int | None, str]]:
        key = (session.id, jid)
        if key not in self._scores:
            self._scores[key] = jmod.load(session, jid, self.index.con)
        return self._scores[key]

    def forget_judgments(self) -> None:
        self._scores.clear()
        self._jid.clear()


# ---------------------------------------------------------------- shared pieces


def _jid(text: str) -> str:
    t = text.strip().lower()
    m = re.fullmatch(r"(?:j_?)?(\d+)", t)
    if not m:
        raise ResumesError("UNKNOWN_JUDGMENT", f"{text}: not a judgment id (expected j_NN)")
    return f"j_{int(m.group(1)):02d}"


def _judgment_for(ctx: Context, session: Session, rs: ResultSet, *, ignore_off: bool = False) -> tuple[str | None, dict[int, tuple[int | None, str]]]:
    """The judgment a set is rendered with: the one it was sorted by, else the newest on its lineage.
    None after `drop rank` / `clear`, until the next `sort --by judgment`."""
    if rs.ranking_off and not ignore_off:
        return None, {}
    key = (session.id, rs.id)
    if key not in ctx._jid:
        ctx._jid[key] = rs.judgment if any(k.startswith("judgment") for k in rs.sort) and rs.judgment \
            else jmod.newest_on_lineage(session, session.lineage(rs.id), ctx.index.con)
    jid = ctx._jid[key]
    return (jid, ctx.scores(session, jid)) if jid else (None, {})


def _n_judged(rs: ResultSet, scores: dict) -> int:
    return sum(1 for d in rs.order if scores.get(d, (None, ""))[0] is not None)


def _labels(session: Session, rs: ResultSet) -> list[tuple[str, str]] | None:
    """(id, label) of a set's filters; None for an old-format set."""
    if rs.filters is None:
        return None
    out = []
    for fid in rs.filters:
        try:
            out.append((fid, fmod.load(session, fid).label))
        except ResumesError:
            out.append((fid, "?"))
    return out


def _state(ctx: Context, session: Session, rs: ResultSet, *, end: bool = False) -> tuple[str, dict]:
    changed = rs.index_version != ctx.index.version
    jid, scores = _judgment_for(ctx, session, rs)
    judged = _n_judged(rs, scores) if jid else 0
    labels = _labels(session, rs)
    data = {"id": rs.id, "count": rs.count, "cursor": rs.cursor, "page": rs.page_no, "pages": rs.pages, "page_size": rs.page_size,
            "sort": rs.sort, "parent": rs.parent, "op": rs.op, "args": rs.args, "judgment": jid, "judged": judged,
            "filters": None if labels is None else [{"id": i, "label": lb} for i, lb in labels],
            "index_version": rs.index_version, "index_changed": changed, "end_of_set": end}
    return render.state_line(rs, index_changed=changed, end=end, filters=labels, judged=(jid, judged) if jid else None), data


def _versions_of(ctx: Context, doc_nos: list[int]) -> dict[int, int]:
    groups, group_of = ctx.index.dup_groups, ctx.index.dup_group_of
    return {d: len(groups[group_of[d]]) - 1 for d in doc_nos if d in group_of and len(groups[group_of[d]]) > 1}


def _page(ctx: Context, session: Session, rs: ResultSet, start: int, end: int, *, end_of_set: bool = False, note: str | None = None) -> Output:
    """Cards `start:end` (0-based) of a set."""
    docs = rs.order[start:end]
    jid, scores = _judgment_for(ctx, session, rs)
    profiles, cards, versions = ctx.index.profiles(docs), ctx.index.cards(docs), _versions_of(ctx, docs)
    card_lines, link_lines, items = [], [], []
    for i, d in enumerate(docs, start=start + 1):
        p = profiles.get(d)
        if p is None or p.deleted:
            card_lines.append(f"{i}. doc {d}: removed from the index")
            link_lines.append(f"{i}. (removed from the index)")
            continue
        judged = scores.get(d) if d in scores else None
        link = ctx.index.link(p)
        # the note is printed once, on the link line; the card carries the score only
        card_lines.append(render.card_text(cards.get(d, f"{p.id} · (no card)"), (judged[0], "") if judged else None, versions.get(d, 0)))
        link_lines.append(render.link_line(i, p, link, judged, ctx.tty, versions.get(d, 0)))
        items.append({"rank": i, "id": p.id, "doc_no": d, "title": p.title, "years": p.years, "rate": p.rate, "currency": p.currency,
                      "score": judged[0] if judged else None, "note": judged[1] if judged else None, "link": link,
                      "card": cards.get(d), "versions": versions.get(d, 0)})
    footer, state = _state(ctx, session, rs, end=end_of_set)
    header = render.page_header(rs, start, end) + (render.SEP + note if note else "")
    text = render.page_text(header, card_lines, link_lines, footer)
    return Output(text, {"set": rs.id, "count": rs.count, "start": start + 1, "end": end, "judgment": jid, "cards": items, "state": state})


def _show_page(ctx: Context, session: Session, rs: ResultSet, start: int, end: int, page_size: int | None = None, note: str | None = None) -> Output:
    """Show a page and move the cursor to its end."""
    session.save_cursor(rs, end, page_size)
    return _page(ctx, session, rs, start, end, note=note)


def _facets_block(ctx: Context, session: Session, rs: ResultSet, head: list[str], strong=None) -> tuple[list[str], dict, dict]:
    f = facets_mod.compute(ctx.index, rs.bitmap(), strong)
    footer, state = _state(ctx, session, rs)
    return [*head, *render.facet_lines(f), render.next_hint(rs, ctx.cfg.query.max_cards), footer], f, state


def _count_and_facets(ctx: Context, session: Session, rs: ResultSet, m: Membership, include_unknown: bool) -> Output:
    head = [render.count_header(rs, m, ctx.index.vocab, include_unknown), *render.resolution_lines(m)]
    lines, f, state = _facets_block(ctx, session, rs, head, m.strong)
    data = {"set": rs.id, "count": rs.count, "resolved": [dict(r.__dict__) for r in m.resolved], "unresolved": m.unresolved,
            "unknown_years": m.unknown_years, "unknown_rate": m.unknown_rate, "facets": f, "state": state}
    if m.like is not None:
        data["like"] = {"source": m.like.source, "requirements": len(m.like.requirements), "resolved": m.like.resolved, "unresolved": m.like.unresolved}
    return Output("\n".join(lines), data)


def _make_set(ctx: Context, session: Session, *, parent: ResultSet | None, op: str, args: dict, filters: list[Filter], sort: list[str],
              jid: str | None, scores: dict | None, ranking_off: bool, page_size: int | None) -> tuple[ResultSet, str | None]:
    """(the new set, the id of the set whose saved order it reuses or None)."""
    order, reused, key = view.materialize(ctx.index, session, filters, sort, jid, scores)
    rs = session.new_set(parent=parent.id if parent else None, op=op, args=args, index_version=ctx.index.version, order=order,
                         judgment=jid, sort=sort, page_size=page_size or (parent.page_size if parent else ctx.cfg.query.page_size),
                         filters=[f.id for f in filters], ranking_off=ranking_off)
    if reused is None:
        session.remember_view(key, rs.id)
    return rs, reused


def _current(ctx: Context, set_arg: str | None = None) -> tuple[Session, ResultSet]:
    """The session and the set a verb acts on; before any search, all CVs."""
    session = ctx.session(create=True)
    if store.parse_set_id(set_arg) is None and not session.meta.get("current"):
        rs, _ = _make_set(ctx, session, parent=None, op="all", args={}, filters=[], sort=[], jid=None, scores=None,
                          ranking_off=False, page_size=None)
        return session, rs
    return session, session.resolve(set_arg)


def _add(ctx: Context, session: Session, *, parent: ResultSet | None, op: str, crit: Criteria, said: str | None,
         page_size: int | None) -> tuple[ResultSet, Membership, str | None, list[Filter]]:
    """Save the conditions of one call as filters and make the set."""
    crit.like_session_dir = session.dir
    parts = view.split(crit)
    if parent is not None and not parts:
        raise ResumesError("NO_CRITERIA", "filter needs --topic/--skill/--text/--like or a constraint (--min-years, --rate-max, …)")
    base = view.filters_of(ctx.index, ctx.cfg, session, parent) if parent is not None else []
    new = [view.ensure(ctx.index, ctx.cfg, session, p, said)[0] for p in parts]
    have = {f.id for f in base}
    filters = base + [f for f in new if f.id not in have]
    jid, scores = _judgment_for(ctx, session, parent) if parent is not None else (None, {})
    rs, reused = _make_set(ctx, session, parent=parent, op=op, args={**crit.to_args(), **view.resolution_of(new)}, filters=filters,
                           sort=list(parent.sort) if parent is not None else [], jid=jid, scores=scores,
                           ranking_off=parent.ranking_off if parent is not None else False, page_size=page_size)
    return rs, view.shown_membership(ctx.index, filters, new, rs.order), reused, new


def _too_many(ctx: Context, session: Session, rs: ResultSet) -> ResumesError:
    limit = ctx.cfg.query.max_cards
    active = view.filters_of(ctx.index, ctx.cfg, session, rs)
    sugg = narrow.suggestions(ctx.index, view.raw_members(ctx.index, active), active, limit)
    msg = "\n".join([
        f"{rs.count} > {limit} — tell the user the set is too big to rank and ask them to narrow it first",
        narrow.line(sugg, limit, render.rate_symbol(ctx.cfg.currency)),
        "listing works at any size: `resumes next`",
    ])
    return ResumesError("TOO_MANY_TO_RANK", msg, {"count": rs.count, "limit": limit, "suggestions": [{**s, "call": narrow.call_of(s)} for s in sugg]})


# ---------------------------------------------------------------- search / filter / drop / clear / sort


def search(ctx: Context, crit: Criteria, *, show: bool | None = None, page_size: int | None = None, said: str | None = None) -> Output:
    """A new question: the view becomes the conditions of this call, with no ranking."""
    session = ctx.session(create=True)
    rs, m, reused, new = _add(ctx, session, parent=None, op="search", crit=crit, said=said, page_size=page_size)
    out = _count_and_facets(ctx, session, rs, m, crit.include_unknown)
    out.data["reused"] = reused
    if show:
        page = _show_page(ctx, session, rs, 0, min(rs.page_size, rs.count))
        return Output(out.text.rsplit("\n", 2)[0] + "\n" + page.text, {**out.data, "page": page.data})
    return out


def filter_(ctx: Context, set_arg: str | None, crit: Criteria, *, show: bool | None = None, page_size: int | None = None,
            said: str | None = None) -> Output:
    """Add conditions to a view. Order and judgments carry over."""
    session, parent = _current(ctx, set_arg)
    parent_jid, _ = _judgment_for(ctx, session, parent)
    rs, m, reused, new = _add(ctx, session, parent=parent, op="filter", crit=crit, said=said, page_size=page_size)
    # a page when the parent was already shown or judged, facets otherwise
    print_page = (parent.cursor > 0 or parent_jid is not None) if show is None else show
    if print_page and rs.count:
        head = render.count_header(rs, m, ctx.index.vocab, crit.include_unknown)
        page = _show_page(ctx, session, rs, 0, min(rs.page_size, rs.count))
        text = "\n".join([head, *render.resolution_lines(m), page.text])
        return Output(text, {**page.data, "resolved": [dict(r.__dict__) for r in m.resolved], "unresolved": m.unresolved,
                             "unknown_years": m.unknown_years, "unknown_rate": m.unknown_rate, "reused": reused})
    out = _count_and_facets(ctx, session, rs, m, crit.include_unknown)
    out.data["reused"] = reused
    return out


def _after_removal(ctx: Context, session: Session, parent: ResultSet, parent_judged: bool, rs: ResultSet, reused: str | None,
                   head: str, show: bool | None) -> Output:
    print_page = (parent.cursor > 0 or parent_judged) if show is None else show
    if print_page and rs.count:
        page = _show_page(ctx, session, rs, 0, min(rs.page_size, rs.count))
        return Output(head + "\n" + page.text, {**page.data, "reused": reused})
    lines, f, state = _facets_block(ctx, session, rs, [head])
    return Output("\n".join(lines), {"set": rs.id, "count": rs.count, "facets": f, "state": state, "reused": reused})


def drop(ctx: Context, targets: list[str], *, show: bool | None = None, page_size: int | None = None) -> Output:
    """Remove filters (by id or by a word of them) and/or the ranking (`rank`). The others stay; nothing is searched."""
    session, parent = _current(ctx)
    if not targets:
        raise ResumesError("NO_TARGET", "say what to remove: a filter id (`f2`), a word of it (`elixir`), or `rank`")
    active = view.filters_of(ctx.index, ctx.cfg, session, parent)
    remove: list[Filter] = []
    rank = False
    for t in targets:
        if t.strip().lower() in view.RANK_WORDS or re.fullmatch(r"j_?\d+", t.strip().lower()):
            rank = True
            continue
        f = view.match(active, t)
        if f.id not in {r.id for r in remove}:
            remove.append(f)
    jid, scores = _judgment_for(ctx, session, parent)
    if rank and not parent.sort and jid is None:
        raise ResumesError("NO_RANKING", f"{parent.id} has no ranking or sort to remove")
    gone = {f.id for f in remove}
    labels = [f"{f.id} {f.label}" for f in remove] + (["the ranking"] if rank else [])
    rs, reused = _make_set(ctx, session, parent=parent, op="drop", args={"removed": labels}, filters=[f for f in active if f.id not in gone],
                           sort=[] if rank else list(parent.sort), jid=None if rank else jid, scores={} if rank else scores,
                           ranking_off=True if rank else parent.ranking_off, page_size=page_size)
    head = f"{rs.id}{render.SEP}{render.people(rs.count)}{render.SEP}removed {', '.join(labels)}"
    return _after_removal(ctx, session, parent, jid is not None, rs, reused, head, show)


def clear(ctx: Context, *, show: bool | None = None, page_size: int | None = None) -> Output:
    """Start over inside the session: no filters, no ranking → all CVs, newest first."""
    session, parent = _current(ctx)
    jid, _ = _judgment_for(ctx, session, parent)
    rs, reused = _make_set(ctx, session, parent=parent, op="clear", args={}, filters=[], sort=[], jid=None, scores={},
                           ranking_off=True, page_size=page_size)
    head = f"{rs.id}{render.SEP}{render.people(rs.count)}{render.SEP}all CVs (filters and ranking removed)"
    return _after_removal(ctx, session, parent, jid is not None, rs, reused, head, show)


def filters_(ctx: Context) -> Output:
    session, rs = _current(ctx)
    active = view.filters_of(ctx.index, ctx.cfg, session, rs)
    lines, rows = [], []
    for f in active:
        alone = view.people(ctx.index, f.members)
        without = view.people(ctx.index, view.raw_members(ctx.index, [g for g in active if g.id != f.id]))
        said = f'{render.SEP}"{f.said}"' if f.said else ""
        lines.append(f"{f.id}  {f.label}{render.SEP}{alone} alone{render.SEP}without it: {without}{said}")
        rows.append({"id": f.id, "label": f.label, "kind": f.kind, "alone": alone, "without": without, "said": f.said, "args": f.args})
    if not active:
        lines.append("no filters: all CVs")
    jid, scores = _judgment_for(ctx, session, rs)
    ranking = None
    if jid:
        criterion = jmod.criterion_of(session, jid, ctx.index.con) or ""
        judged = _n_judged(rs, scores)
        s = render.sort_label(rs)
        lines.append(f'rank  {jid} "{criterion}"{render.SEP}{judged} of {rs.count} judged' + (render.SEP + s if s else ""))
        ranking = {"judgment": jid, "criterion": criterion, "judged": judged, "sort": rs.sort}
    elif rs.sort:
        lines.append("sort  " + render.sort_label(rs)[7:])
        ranking = {"judgment": None, "sort": rs.sort}
    hints = ([f"remove one: `resumes drop {active[-1].id}`", "all: `resumes clear`"] if active else []) + (["the ranking: `resumes drop rank`"] if ranking else [])
    if hints:
        lines.append(render.SEP.join(hints))
    footer, state = _state(ctx, session, rs)
    lines.append(footer)
    return Output("\n".join(lines), {"set": rs.id, "count": rs.count, "filters": rows, "ranking": ranking, "state": state})


def sort_(ctx: Context, set_arg: str | None, by: str, *, judgment: str | None = None, show: bool | None = None, page_size: int | None = None) -> Output:
    session, parent = _current(ctx, set_arg)
    keys = parse_keys(by)
    by_judgment = any(k == "judgment" for k, _ in keys)
    jid, scores = _judgment_for(ctx, session, parent, ignore_off=by_judgment)
    if by_judgment:
        if judgment:
            jid = _jid(judgment)
            scores = ctx.scores(session, jid)
            if not scores:
                raise ResumesError("UNKNOWN_JUDGMENT", f"{jid} (have {', '.join(sorted({j['id'] for j in jmod.list_judgments(session, ctx.index.con)})) or 'none'})")
        elif jid is None:
            raise ResumesError("NO_JUDGMENT", f"{parent.id} has no judgment yet: `resumes cards {parent.id}`, then `resumes score {parent.id} --from scores.json`")
    args = {"by": [k for k, _ in keys]}
    if judgment:
        args["judgment"] = jid
    rs, reused = _make_set(ctx, session, parent=parent, op="sort", args=args, filters=view.filters_of(ctx.index, ctx.cfg, session, parent),
                           sort=[f"{k}:{d}" for k, d in keys], jid=jid if by_judgment else (jid or parent.judgment), scores=scores,
                           ranking_off=False if by_judgment else parent.ranking_off, page_size=page_size)
    if show is False or not rs.count:
        footer, state = _state(ctx, session, rs)
        return Output(f"{rs.id}{render.SEP}{render.people(rs.count)}{render.SEP}{render.sort_label(rs)}\n{footer}",
                      {"set": rs.id, "count": rs.count, "state": state, "reused": reused})
    out = _show_page(ctx, session, rs, 0, min(rs.page_size, rs.count))
    out.data["reused"] = reused
    return out


# ---------------------------------------------------------------- cards / score


ANCHORS = 3


def cards(ctx: Context, set_arg: str | None, *, new: bool = False) -> Output:
    """The cards to judge (at most `max_cards` people). Under an active ranking only the unjudged are printed;
    `new` prints everyone, for another criterion."""
    session, rs = _current(ctx, set_arg)
    if rs.count > ctx.cfg.query.max_cards:
        raise _too_many(ctx, session, rs)
    jid, scores = (None, {}) if new else _judgment_for(ctx, session, rs)
    judged = [d for d in rs.order if scores.get(d, (None, ""))[0] is not None]
    docs = [d for d in rs.order if scores.get(d, (None, ""))[0] is None] if jid else list(rs.order)
    f = facets_mod.compute(ctx.index, rs.bitmap())
    p50 = f.get("rate", {}).get("p50")
    rate_txt = f"rate p50 {render.rate_symbol(f['rate']['currency'])}{p50}/h" if p50 is not None else "rate ?"
    criterion = jmod.criterion_of(session, jid, ctx.index.con) if jid else None
    footer, state = _state(ctx, session, rs)
    base = {"set": rs.id, "count": rs.count, "rate_p50": p50, "scores_path": str(session.scores_path), "judgment": jid,
            "criterion": criterion, "judged": len(judged), "state": state}
    if jid and not docs:
        text = "\n".join([f'{rs.id}{render.SEP}{render.people(rs.count)}{render.SEP}all judged under {jid} "{criterion}"{render.SEP}nothing to score',
                          f"next: `resumes sort {rs.id} --by judgment,rate`{render.SEP}another criterion: `resumes cards {rs.id} --new`", footer])
        return Output(text, {**base, "shown": 0, "cards": [], "anchors": []})
    if jid:
        label = f"{len(docs)} cards ({len(judged)} of {rs.count} already judged)" if judged else f"{len(docs)} cards"
        head = render.SEP.join([rs.id, label, rate_txt, f'same criterion as {jid} "{criterion}"',
                                f'write scores to {session.scores_path} with "judgment":"{jid}"'])
    else:
        head = render.SEP.join([rs.id, f"{rs.count} cards", rate_txt, f"write scores to {session.scores_path}"])
    lines = [head]
    anchors: list[int] = []
    if jid:
        ranked = sorted(((s, d) for d, (s, _) in scores.items() if s is not None), key=lambda x: (-x[0], x[1]))
        anchors = [d for _, d in ranked[:ANCHORS]] + [d for _, d in ranked[-ANCHORS:] if d not in {x for _, x in ranked[:ANCHORS]}]
    texts, versions = ctx.index.cards(docs + anchors), _versions_of(ctx, docs + anchors)
    if anchors:
        lines.append(f"anchors (judged before under {jid}: keep your scores consistent with these; do not score them again):")
        lines += [render.card_text(texts[d], scores[d], versions.get(d, 0)).split("\n", 1)[0] for d in anchors if d in texts]
        lines.append(f"other criterion than {jid}? → `resumes cards {rs.id} --new`")
    lines.append("cards:")
    items, body = [], []
    for d in docs:
        t = texts.get(d)
        if t is None:
            body.append(f"doc {d}: removed from the index")
            continue
        body.append(render.card_text(t, None, versions.get(d, 0)))
        items.append({"doc_no": d, "id": t.split(" · ", 1)[0], "card": t, "score": None, "note": None})
    lines.append("\n\n".join(body))
    lines.append(footer)
    ids = ctx.index.ids(anchors)
    return Output("\n".join(lines), {**base, "shown": len(docs), "cards": items,
                                     "anchors": [{"doc_no": d, "id": ids.get(d), "score": scores[d][0], "note": scores[d][1]} for d in anchors]})


def read_scores_source(session: Session, source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    p = Path(source)
    candidates = [p] if p.is_absolute() else [session.dir / p, p]
    for c in candidates:
        if c.is_file():
            return c.read_text(encoding="utf-8")
    raise ResumesError("SCORES_FILE_NOT_FOUND", f"{source} (looked in {session.dir} and the working directory)")


def _merge_target(ctx: Context, session: Session, rs: ResultSet, text: str, into: str | None, criterion: str | None) -> str | None:
    """The judgment these scores add to: `--into`, else the file's "judgment", else the set's ranking when the
    criterion is the same. None: a new judgment."""
    if into:
        return _jid(into)
    try:
        payload = json.loads(text)
    except ValueError:
        return None                                  # `record` reports the bad file
    if not isinstance(payload, dict):
        return None
    if payload.get("judgment"):
        return _jid(str(payload["judgment"]))
    jid, _ = _judgment_for(ctx, session, rs, ignore_off=True)
    if jid and jmod.same_criterion(criterion or payload.get("criterion"), jmod.criterion_of(session, jid, ctx.index.con)):
        return jid
    return None


def score(ctx: Context, set_arg: str | None, *, source: str | None = None, text: str | None = None, allow_partial: bool = False,
          criterion: str | None = None, judge: str | None = None, into: str | None = None) -> Output:
    """Record a judgment from `source` (a path, a name in the session dir, or `-`) or from `text`.
    Scores for an existing judgment's criterion are added to it; people it already covers keep their score."""
    session, rs = _current(ctx, set_arg)
    if text is None:
        text = read_scores_source(session, source or "scores.json")
    target = _merge_target(ctx, session, rs, text, into, criterion)
    known = list(rs.order) + (list(ctx.scores(session, target)) if target else [])     # so an anchor echoed back is recognised and skipped
    id_to_docno = {v: k for k, v in ctx.index.ids(known).items()}
    try:
        st = jmod.record(session, rs, text, id_to_docno, allow_partial=allow_partial, criterion=criterion, judge=judge, into=target)
    except ResumesError as e:
        if e.code == "SCORES_INCOMPLETE" and target is None:
            jid, scores = _judgment_for(ctx, session, rs, ignore_off=True)
            if jid and _n_judged(rs, scores):
                e.message += f'; to add these scores to {jid} put "judgment":"{jid}" in the file'
                raise ResumesError(e.code, e.message) from None
        raise
    ctx.forget_judgments()
    total = _n_judged(rs, ctx.scores(session, st.judgment_id))
    parts = [st.judgment_id, rs.id, f"{st.n_scored} scored"]
    if st.merged:
        parts.append(f"added to {st.judgment_id}: {total} of {rs.count} judged")
    if st.n_kept:
        parts.append(f"{st.n_kept} already judged (their score stands)")
    if st.n_missing:
        parts.append(f"{st.n_missing} unscored (sort last)")
    if st.p50 is not None:
        parts.append(f"p50 {st.p50:g}")
    if st.top:
        parts.append(f"top {st.top[0]} ★{st.top[1]}")
    parts.append(f'criterion "{st.criterion}"')
    lines = [render.SEP.join(parts)]
    notes = []
    if st.n_clamped:
        notes.append(f"{st.n_clamped} score(s) clamped to 0–100")
    if st.n_truncated:
        notes.append(f"{st.n_truncated} note(s) cut to {jmod.NOTE_MAX} chars")
    if notes:
        lines.append("note: " + render.SEP.join(notes))
    lines.append(f"next: `resumes sort {rs.id} --by judgment,rate`")
    return Output("\n".join(lines), {"judgment": st.judgment_id, "set": rs.id, "criterion": st.criterion, "judge": st.judge, "n_scored": st.n_scored,
                                     "n_missing": st.n_missing, "n_clamped": st.n_clamped, "n_truncated": st.n_truncated, "n_kept": st.n_kept,
                                     "merged": st.merged, "judged": total, "p50": st.p50,
                                     "top": {"id": st.top[0], "score": st.top[1]} if st.top else None})


# ---------------------------------------------------------------- paging


def next_(ctx: Context, *, page_size: int | None = None) -> Output:
    session, rs = _current(ctx)
    if page_size:
        rs.page_size = page_size
    if rs.cursor >= rs.count:
        footer, state = _state(ctx, session, rs, end=True)
        text = f"{rs.id}{render.SEP}{render.people(rs.count)}{render.SEP}end of set ({rs.count} shown){render.SEP}`resumes prev` or `resumes page 1`\n{footer}"
        return Output(text, {"set": rs.id, "count": rs.count, "end_of_set": True, "cards": [], "state": state})
    return _show_page(ctx, session, rs, rs.cursor, min(rs.cursor + rs.page_size, rs.count), page_size)


def prev_(ctx: Context) -> Output:
    session, rs = _current(ctx)
    if rs.count == 0:
        raise ResumesError("EMPTY_SET", rs.id)
    target = max(1, rs.page_no - 1)
    note = "already at the first page" if rs.page_no <= 1 else None
    return _show_page(ctx, session, rs, (target - 1) * rs.page_size, min(target * rs.page_size, rs.count), note=note)


def page(ctx: Context, n: int) -> Output:
    session, rs = _current(ctx)
    if rs.count == 0:
        raise ResumesError("EMPTY_SET", rs.id)
    if n < 1 or n > rs.pages:
        raise ResumesError("NO_SUCH_PAGE", f"{n} (have 1..{rs.pages})")
    return _show_page(ctx, session, rs, (n - 1) * rs.page_size, min(n * rs.page_size, rs.count))


def top(ctx: Context, n: int) -> Output:
    session, rs = _current(ctx)
    if n < 1:
        raise ResumesError("BAD_ARGUMENT", "top N needs N ≥ 1")
    if rs.count == 0:
        raise ResumesError("EMPTY_SET", rs.id)
    return _show_page(ctx, session, rs, 0, min(n, rs.count))


def _current_view(ctx: Context, session: Session, rs: ResultSet) -> Output:
    """A set's last shown page, or its state when nothing was shown yet."""
    if rs.cursor == 0 or rs.count == 0:
        footer, state = _state(ctx, session, rs)
        return Output(f"{rs.id}{render.SEP}{render.people(rs.count)}{render.SEP}nothing shown yet (`resumes next`)\n{footer}", {"set": rs.id, "count": rs.count, "cards": [], "state": state})
    start = (rs.page_no - 1) * rs.page_size
    return _page(ctx, session, rs, start, min(rs.cursor, rs.count))


def back(ctx: Context) -> Output:
    session, rs = _current(ctx)
    if not rs.parent:
        raise ResumesError("NO_PARENT", f"{rs.id} is a root set")
    parent = session.get(rs.parent)
    session.set_current(parent.id)
    return _current_view(ctx, session, parent)


def use(ctx: Context, set_arg: str) -> Output:
    session, rs = _current(ctx, set_arg)
    session.set_current(rs.id)
    return _current_view(ctx, session, rs)


def sets(ctx: Context) -> Output:
    session = ctx.session(create=True)
    all_sets = session.all_sets()
    current = session.meta.get("current")
    by_id = {r.id: r for r in all_sets}
    children: dict[str | None, list[ResultSet]] = {}
    for r in all_sets:
        children.setdefault(r.parent if r.parent in by_id else None, []).append(r)
    judged: dict[str, str] = {}
    for j in jmod.list_judgments(session, ctx.index.con):
        judged.setdefault(j["set_id"], j["id"])
    lines: list[str] = []
    rows: list[dict] = []

    def walk(parent: str | None, depth: int) -> None:
        for r in children.get(parent, []):
            desc = describe_args(r.args)
            bits = [f"{r.op} {desc}".strip()]
            if r.id in judged:
                bits.append(judged[r.id])
            if r.cursor:
                bits.append(f"shown {r.cursor}")
            prefix = ("  " * (depth - 1) + "└ ") if depth else ""
            mark = "  ← current" if r.id == current else ""
            lines.append(f"{prefix}{r.id}  {r.count:>5}  {render.SEP.join(bits)}{mark}")
            rows.append({"id": r.id, "parent": r.parent, "count": r.count, "op": r.op, "args": r.args, "cursor": r.cursor,
                         "judgment": judged.get(r.id), "current": r.id == current, "filters": r.filters})
            walk(r.id, depth + 1)

    walk(None, 0)
    if not lines:
        lines.append("no sets yet")
    lines.append(f"— session {session.id}{render.SEP}{len(all_sets)} sets{render.SEP}current {current or '—'}")
    return Output("\n".join(lines), {"session": session.id, "current": current, "sets": rows})


# ---------------------------------------------------------------- set algebra


def _algebra(ctx: Context, op: str, a_arg: str, b_arg: str) -> Output:
    session = ctx.session()
    a, b = session.resolve(a_arg), session.resolve(b_arg)
    in_b = set(b.order)
    order = a.order + [d for d in b.order if d not in set(a.order)] if op == "union" else [d for d in a.order if d not in in_b]
    jid, _ = _judgment_for(ctx, session, a)
    label = f"{a.id} {'∪' if op == 'union' else '−'} {b.id}"
    # not an intersection of conditions, so it becomes one frozen filter
    fz = fmod.frozen(session, label=label, order=order, index_version=ctx.index.version)
    rs = session.new_set(parent=a.id, op=op, args={"with": b.id}, index_version=ctx.index.version, order=order,
                         judgment=jid, sort=a.sort if op == "minus" else [], page_size=a.page_size, filters=[fz.id], ranking_off=a.ranking_off)
    head = f"{rs.id}{render.SEP}{render.people(rs.count)}{render.SEP}{label}"
    lines, f, state = _facets_block(ctx, session, rs, [head])
    return Output("\n".join(lines), {"set": rs.id, "count": rs.count, "facets": f, "state": state})


def union(ctx: Context, a: str, b: str) -> Output:
    return _algebra(ctx, "union", a, b)


def minus(ctx: Context, a: str, b: str) -> Output:
    return _algebra(ctx, "minus", a, b)


# ---------------------------------------------------------------- server-side judge


def judge(ctx: Context, set_arg: str | None, *, criterion: str, model: str = "gpt-5-mini", batch: int = 25, log=None) -> Output:
    from ..judge.openai import judge_cards

    session, rs = _current(ctx, set_arg)
    if rs.count > ctx.cfg.query.max_cards:
        raise _too_many(ctx, session, rs)
    texts = ctx.index.cards(rs.order)
    ids = ctx.index.ids(rs.order)
    cards_in = [(ids[d], render.card_text(texts[d], None)) for d in rs.order if d in texts]
    scores, usage = judge_cards(cards_in, criterion, model=model, batch=batch, log=log)
    payload = json.dumps({"criterion": criterion, "judge": f"{model} via resumes judge", "scores": scores}, ensure_ascii=False)
    out = score(ctx, rs.id, text=payload, allow_partial=len(scores) < len(cards_in))
    out.text += f"\njudge: {model}{render.SEP}{usage['calls']} call(s){render.SEP}{usage['prompt_tokens']} in / {usage['completion_tokens']} out tokens"
    out.data.update({"model": model, "usage": usage})
    return out


# ---------------------------------------------------------------- show / vocab


def show(ctx: Context, ref: str, *, full: bool = False, contact: bool = False) -> Output:
    p = ctx.index.doc(ref)
    judged = None
    try:
        session = ctx.session()
        rs = session.current()
        if rs is not None and p.doc_no in set(rs.order):
            _, scores = _judgment_for(ctx, session, rs)
            judged = scores.get(p.doc_no)
    except ResumesError:
        pass
    link = ctx.index.link(p)
    if contact:
        if not ctx.cfg.query.allow_contact:
            raise ResumesError("CONTACT_DISABLED", "set allow_contact = true under [query] in resumes.toml to read the unredacted source")
        return Output(_raw_source(ctx, p), {"id": p.id, "doc_no": p.doc_no, "link": link, "contact": True})
    if full:
        md = ctx.index.markdown(p.doc_no) or ""
        return Output(md.rstrip() + f"\n\n→ {render.osc8(link, ctx.tty)}", {"id": p.id, "doc_no": p.doc_no, "link": link, "markdown": md})
    card = ctx.index.cards([p.doc_no]).get(p.doc_no, f"{p.id} · (no card)")
    versions = _versions_of(ctx, [p.doc_no]).get(p.doc_no, 0)
    text = render.card_text(card, judged, versions, link=link)
    return Output(text, {"id": p.id, "doc_no": p.doc_no, "card": card, "link": link, "score": judged[0] if judged else None,
                         "note": judged[1] if judged else None, "versions": versions})


def _raw_source(ctx: Context, p: Profile) -> str:
    from ..ingest.corpus import _load_source

    src, sid = ctx.index.con.execute("SELECT source, source_id FROM docs WHERE doc_no = ?", [p.doc_no]).fetchone()
    for s in ctx.cfg.sources:
        if s.id != src:
            continue
        for raw in _load_source(s):
            if str(raw.source_id) == str(sid):
                return raw.markdown
    raise ResumesError("SOURCE_NOT_FOUND", f"{src}:{sid} is not in the configured sources any more")


def vocab_review(ctx: Context, top: int = 30) -> Output:
    from ..vocab.review import review

    rows = review(ctx.cfg.index.vocab, ctx.cfg.index.cache / "unresolved_terms.json", top)
    lines = [f"{r['name']:<40} {r['count']:>5}  " + (f"already: {r['already']}" if r["already"] else (f"nearest: {r['nearest'][0]} ({r['nearest'][1]})" if r["nearest"] else "—")) for r in rows]
    lines.append(f"— {len(rows)} of the queued unresolved names{render.SEP}`resumes vocab add|alias|merge …` edits schema/vocab.csv{render.SEP}then `resumes index build`")
    return Output("\n".join(lines), {"unresolved": rows})


def vocab_edit(ctx: Context, action: str, **kw) -> Output:
    from ..vocab import review as vr

    path = ctx.cfg.index.vocab
    if action == "add":
        row = vr.add(path, **kw)
    elif action == "alias":
        row = vr.alias(path, kw["slug"], kw["form"])
    else:
        row = vr.merge(path, kw["src"], kw["dst"])
    return Output(f"{action} {row['slug']}{render.SEP}{row['kind']}{render.SEP}{row['canonical']}{render.SEP}aliases: {row['aliases'] or '—'}{render.SEP}implies: {row['implies'] or '—'}\nrebuild to apply: `resumes index build` (the index carries vocab_hash {ctx.index.meta.get('vocab_hash')})", {"row": row})


def vocab(ctx: Context, term: str) -> Output:
    from .terms import resolve_one, suggestion

    v = ctx.index.vocab
    found = resolve_one(v, term, "any")
    if not found:
        s = suggestion(v, term)
        hint = f" → did you mean {s[0]} ({s[1]:.2f})?" if s else ""
        raise ResumesError("UNRESOLVED_TERM", f'"{term}"{hint}')
    lines, rows = [], []
    for r in found:
        t = v.by_slug[r.slug]
        strong, used, weak = ctx.index.bitmaps(t.slug)
        al = ", ".join(t.aliases) if t.aliases else "—"
        amb = f" (ambiguous: {', '.join(sorted(t.ambiguous))})" if t.ambiguous else ""
        lines.append(f"{t.slug}{render.SEP}{t.kind}{render.SEP}{t.canonical}{render.SEP}aliases: {al}{amb}")
        lines.append(f"implies: {', '.join(t.implies) or '—'}{render.SEP}implied by: {', '.join(v.implying(t.slug)) or '—'}")
        lines.append(f"members: strong {len(strong)}{render.SEP}used {len(used)}{render.SEP}weak {len(weak)}")
        if t.description:
            lines.append(f"description: {t.description[:200]}")
        rows.append({"slug": t.slug, "kind": t.kind, "canonical": t.canonical, "aliases": t.aliases, "ambiguous": sorted(t.ambiguous),
                     "implies": t.implies, "implied_by": v.implying(t.slug), "strong": len(strong), "used": len(used), "weak": len(weak),
                     "description": t.description})
    return Output("\n".join(lines), {"terms": rows})


# ---------------------------------------------------------------- sessions


def session_new(ctx: Context, sid: str | None = None, *, make_current: bool = True) -> Output:
    s = store.new_session(ctx.sessions_dir, ctx.index.version, sid, make_current=make_current)
    warn = f"\nnote: $RESUMES_SESSION is set and overrides {store.CURRENT_FILE}" if store.current_session_id(ctx.sessions_dir)[1] == "env" else ""
    return Output(f"session {s.id}{render.SEP}{s.dir}{warn}", {"session": s.id, "dir": str(s.dir)})


def session_use(ctx: Context, sid: str) -> Output:
    s = store.use_session(ctx.sessions_dir, sid)
    return Output(f"session {s.id}{render.SEP}current {s.meta.get('current') or '—'}", {"session": s.id, "current": s.meta.get("current")})


def session_list(ctx: Context) -> Output:
    active, source = store.current_session_id(ctx.sessions_dir, ctx.session_id)
    rows = store.list_sessions(ctx.sessions_dir)
    lines = [f"{r['id']}  sets {r['sets']}  judgments {r['judgments']}  current {r['current'] or '—'}  updated {r['updated_at']}" + ("  ← active" if r["id"] == active else "") for r in rows]
    if not lines:
        lines.append("no sessions")
    lines.append(f"— active: {active or 'none'} ({source})")
    return Output("\n".join(lines), {"active": active, "source": source, "sessions": rows})


def session_gc(ctx: Context, days: int | None = None) -> Output:
    days = ctx.cfg.query.gc_days if days is None else days
    active, _ = store.current_session_id(ctx.sessions_dir, ctx.session_id)
    removed = store.gc_sessions(ctx.sessions_dir, days, keep=active)
    return Output(f"removed {len(removed)} session(s) older than {days} days" + (": " + ", ".join(removed) if removed else ""), {"removed": removed, "days": days})
