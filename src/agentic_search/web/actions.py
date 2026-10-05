"""The mechanical steps of the panel and the slash commands: no model. Each step leaves one line in the
chat ("you removed skill: Elixir → 165 people") so the transcript tells the whole story."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import ResumesError
from ..query import facets as facets_mod
from ..query import verbs, view
from ..query.search import Criteria, availability_label, places
from ..query.sort import DEFAULT_DIR
from ..query.verbs import Context
from ..session.filters import Filter
from ..session.store import ResultSet
from . import state
from .state import chip_text, fmt, people  # noqa: F401
from .store import WebSession

SENIORITIES = tuple(facets_mod.SENIORITY_RANK)
CHANGES = {"filter", "search", "drop", "clear", "sort", "undo", "use", "rank_off", "rank_on", "unknown", "jd"}     # steps that make or switch the set: refused while a ranking runs
HELP = ("/next · /prev · /page N · /top N · /show ID or #N · /filters · /drop WORD or f2 or rank · /clear-filters · "
        "/sort rate,years · /back · /sets · /rank CRITERION")


# ---------------------------------------------------------------- conditions


def _list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def _number(args: dict, key: str) -> float | None:
    v = args.get(key)
    if v is None or v == "":
        return None
    try:
        n = float(str(v).replace("€", "").replace(",", ".").strip())
    except ValueError:
        raise ResumesError("BAD_ARGUMENT", f"{key.replace('_', ' ')}: {v!r} is not a number") from None
    if n < 0:
        raise ResumesError("BAD_ARGUMENT", f"{key.replace('_', ' ')} cannot be negative")
    return n


def criteria_from(args: dict) -> Criteria:
    args = dict(args or {})
    mode: str | int = "all"
    m = args.get("mode")
    if args.get("any") or m == "any":
        mode = "any"
    elif isinstance(m, int) and not isinstance(m, bool) and m > 0:
        mode = m
    elif isinstance(m, str) and m.isdigit() and int(m) > 0:
        mode = int(m)
    seniority = [s.lower() for s in _list(args.get("seniority"))]
    bad = [s for s in seniority if s not in SENIORITIES]
    if bad:
        raise ResumesError("BAD_ARGUMENT", f"seniority {', '.join(bad)}: use {', '.join(SENIORITIES)}")
    availability = []
    for a in _list(args.get("availability")):
        label = availability_label(a)
        if not label or not re.fullmatch(r"now|\d+[dwm]", label):
            raise ResumesError("BAD_ARGUMENT", f"availability {a!r}: use now, 2w, 1m or 3m")
        availability.append(label)
    text = " ".join(str(args.get("text") or "").split()) or None
    location = places(args.get("location"))
    remote = args.get("remote")
    return Criteria(
        topics=_list(args.get("topic")), skills=_list(args.get("skill")), text=text, mode=mode,
        strict=args.get("strict") if isinstance(args.get("strict"), bool) else None,
        min_years=_number(args, "min_years"), seniority=seniority, rate_max=_number(args, "rate_max"), location=location,
        remote=True if remote is True else None, availability=availability,
        level_used=bool(args.get("level_used")) or args.get("level") == "used", include_unknown=bool(args.get("include_unknown")),
        like=str(args["like"]).strip() if args.get("like") else None,
    )


def split(crit: Criteria) -> list[Criteria]:
    """Unlike `view.split`, each term that must match becomes its own filter, so each can be removed alone."""
    parts: list[Criteria] = []
    if crit.has_terms:
        if crit.mode == "all" and not crit.like:
            parts += [Criteria(topics=[t], strict=crit.strict, level_used=crit.level_used) for t in crit.topics]
            parts += [Criteria(skills=[s], strict=crit.strict, level_used=crit.level_used) for s in crit.skills]
            if crit.text:
                parts.append(Criteria(text=crit.text))
        else:
            parts.append(Criteria(topics=list(crit.topics), skills=list(crit.skills), text=crit.text, mode=crit.mode, strict=crit.strict,
                                  level_used=crit.level_used, like=crit.like))
    rest = Criteria(min_years=crit.min_years, rate_max=crit.rate_max, seniority=list(crit.seniority), availability=list(crit.availability),
                    location=crit.location, remote=crit.remote, include_unknown=crit.include_unknown)
    return parts + view.split(rest)


@dataclass
class Step:
    before: ResultSet
    after: ResultSet
    added: list[Filter] = field(default_factory=list)
    kept: list[Filter] = field(default_factory=list)        # asked for, and active already
    removed: list[Filter] = field(default_factory=list)
    made: bool = True


def _shown(ctx: Context, rs: ResultSet) -> None:
    session = ctx.session()
    if rs.count and rs.cursor == 0:
        session.save_cursor(rs, min(rs.page_size, rs.count))


def apply(ctx: Context, web: WebSession | None, args: dict, *, new_question: bool = False, said: str | None = None,
          replace: str | None = None) -> Step:
    """All conditions are evaluated before the set is made, so a word that does not resolve changes nothing.
    `replace`: the id of an active filter these conditions take the place of."""
    session, parent = verbs._current(ctx)
    crit = criteria_from(args)
    parts = split(crit)
    for p in parts:
        p.like_session_dir = session.dir
    if not parts:
        raise ResumesError("NO_CRITERIA", "say what to filter by: a topic, a skill, a sentence, or a limit (years, rate, location, …)")
    base = [] if new_question else view.filters_of(ctx.index, ctx.cfg, session, parent)
    gone = [f for f in base if f.id == replace]
    base = [f for f in base if f.id != replace]
    wanted: list[Filter] = []
    for p in parts:
        f = view.ensure(ctx.index, ctx.cfg, session, p, said)[0]
        if f.id not in {w.id for w in wanted}:
            wanted.append(f)
    have = {f.id for f in base}
    added = [f for f in wanted if f.id not in have]
    if not added and not gone and not new_question:
        raise ResumesError("ALREADY_ACTIVE", ", ".join(chip_text(state.filter_view(ctx, web, f)) for f in wanted))
    if new_question:
        jid, scores, sort, off = None, {}, [], False
    else:
        jid, scores = verbs._judgment_for(ctx, session, parent)
        sort, off = list(parent.sort), parent.ranking_off
    rs, _ = verbs._make_set(ctx, session, parent=None if new_question else parent, op="search" if new_question else "filter",
                            args={**crit.to_args(), **view.resolution_of(added)}, filters=base + added, sort=sort, jid=jid, scores=scores,
                            ranking_off=off, page_size=parent.page_size)
    _shown(ctx, rs)
    if web is not None:
        web.stepped((parent.id, parent.count), (rs.id, rs.count), made=True)
    return Step(parent, rs, added=added, kept=[f for f in wanted if f.id in have], removed=gone)


def _moved(ctx: Context, web: WebSession | None, before: ResultSet, *, made: bool) -> ResultSet:
    session = ctx.session()
    after = session.current()
    _shown(ctx, after)
    if web is not None:
        web.stepped((before.id, before.count), (after.id, after.count), made=made)
    return after


def remove(ctx: Context, web: WebSession | None, targets: list[str]) -> Step:
    session, before = verbs._current(ctx)
    active = view.filters_of(ctx.index, ctx.cfg, session, before)
    targets = [str(t) for t in targets if str(t).strip()]
    gone = []
    for t in targets:
        if t.strip().lower() in view.RANK_WORDS or re.fullmatch(r"j_?\d+", t.strip().lower()):
            continue
        f = view.match(active, t)
        if f.id not in {g.id for g in gone}:
            gone.append(f)
    verbs.drop(ctx, targets, show=True)
    return Step(before, _moved(ctx, web, before, made=True), removed=gone)


def clear(ctx: Context, web: WebSession | None) -> Step:
    session, before = verbs._current(ctx)
    active = view.filters_of(ctx.index, ctx.cfg, session, before)
    verbs.clear(ctx, show=True)
    return Step(before, _moved(ctx, web, before, made=True), removed=active)


def sort(ctx: Context, web: WebSession | None, by: str, judgment: str | None = None) -> Step:
    _, before = verbs._current(ctx)
    verbs.sort_(ctx, None, by, judgment=judgment, show=True)
    return Step(before, _moved(ctx, web, before, made=True))


def base_order(ctx: Context, web: WebSession | None) -> Step:
    """Back to the order the filters give; the ranking's stars stay."""
    session, before = verbs._current(ctx)
    if not before.sort:
        raise ResumesError("NO_RANKING", "the list is in its base order already")
    jid, scores = verbs._judgment_for(ctx, session, before)
    rs, _ = verbs._make_set(ctx, session, parent=before, op="sort", args={"by": ["relevance"]}, filters=view.filters_of(ctx.index, ctx.cfg, session, before),
                            sort=[], jid=jid or before.judgment, scores=scores, ranking_off=before.ranking_off, page_size=before.page_size)
    return Step(before, _moved(ctx, web, before, made=True))


def goto(ctx: Context, n: int | str) -> ResultSet:
    """Page n, `next` or `prev`; the engine's cursor is the page the user is on."""
    session, rs = verbs._current(ctx)
    if rs.cursor == 0 and rs.count:
        session.save_cursor(rs, min(rs.page_size, rs.count))
    if n == "next":
        n = rs.page_no + 1
    elif n == "prev":
        n = rs.page_no - 1
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ResumesError("BAD_ARGUMENT", f"page {n!r} is not a number") from None
    verbs.page(ctx, n)
    return ctx.session().current()


def undo(ctx: Context, web: WebSession | None) -> Step:
    _, before = verbs._current(ctx)
    target = (web.prev_of(before.id) if web is not None else None) or before.parent
    if not target:
        raise ResumesError("NO_PARENT", "nothing to undo: this is the first step")
    verbs.use(ctx, target)
    return Step(before, _moved(ctx, web, before, made=False), made=False)


def use(ctx: Context, web: WebSession | None, set_arg: str) -> Step:
    _, before = verbs._current(ctx)
    verbs.use(ctx, str(set_arg))
    return Step(before, _moved(ctx, web, before, made=False), made=False)


# ---------------------------------------------------------------- what a step is called in the chat


def step_words(ctx: Context, web: WebSession | None, step: Step) -> str:
    names = lambda fs: ", ".join(chip_text(state.filter_view(ctx, web, f)) for f in fs)      # noqa: E731
    bits = []
    if step.added:
        bits.append("added " + names(step.added))
    if step.removed:
        bits.append("removed " + names(step.removed))
    return " · ".join(bits)


def arrow(ctx: Context, step: Step) -> str:
    return f"→ {people(ctx, step.after.count)}"


def _kept_stars(ctx: Context, step: Step) -> str:
    session = ctx.session()
    jid, scores = verbs._judgment_for(ctx, session, step.after)
    n = verbs._n_judged(step.after, scores) if jid else 0
    return f" · {n} keep their ★" if n and n < step.after.count else ""


# ---------------------------------------------------------------- errors, in words


def friendly(ctx: Context, web: WebSession | None, e: ResumesError) -> dict:
    """The engine's `CODE message` as one line in words, with a fix if there is one."""
    code, msg = e.code, e.message
    text, fix = f"{msg or code}", None
    try:
        session, rs = verbs._current(ctx)
        active = [state.filter_view(ctx, web, f) for f in view.filters_of(ctx.index, ctx.cfg, session, rs)]
    except ResumesError:
        rs, active = None, []
    names = ", ".join(a["value"] for a in active) or "none"
    quoted = re.search(r'"([^"]+)"', msg or "")
    if code in ("UNKNOWN_FILTER", "AMBIGUOUS_FILTER"):
        word = quoted.group(1) if quoted else (msg.split(" ", 1)[0] if msg else "that")
        text = (f"No filter named “{word}”." if code == "UNKNOWN_FILTER" else f"“{word}” fits more than one filter.") + f" Active: {names}."
        if active:
            last = active[-1]
            fix = {"label": f"/drop {last['id']}", "hint": f"remove {chip_text(last)}", "action": {"type": "drop", "targets": [last["id"]]}}
    elif code == "UNRESOLVED_TERM":
        m = re.search(r'"([^"]+)" → did you mean ([^ ]+)', msg or "")
        if m:
            slug = m.group(2)
            t = ctx.index.vocab.by_slug.get(slug)
            name = t.canonical if t else slug
            text = f"No skill or topic “{m.group(1)}” — did you mean {name}?"
            fix = {"label": name, "hint": "use it", "action": {"type": "filter", "args": {("topic" if t is not None and t.kind == "topic" else "skill"): [name]}}}
    elif code == "NO_SUCH_PAGE" and rs is not None:
        text = f"There is no page {msg.split(' ', 1)[0]} — the list has {rs.pages}."
        if rs.pages:
            fix = {"label": f"/page {rs.pages}", "hint": "the last page", "action": {"type": "page", "n": rs.pages}}
    elif code == "NO_SUCH_RANK":
        text = f"There is no {msg.split(' ', 1)[0]} — the list has {fmt(rs.count) if rs is not None else '?'}."
    elif code == "EMPTY_SET":
        text = "The list is empty — remove a filter first."
    elif code in ("NO_RANKING", "NO_JUDGMENT"):
        text = "Nothing is ranked or sorted yet." if code == "NO_RANKING" else "Nothing is ranked yet — ask me to rank them."
    elif code == "NO_PARENT":
        text = "Nothing to undo — this is the first step."
    elif code == "ALREADY_ACTIVE":
        text = f"Already filtered by {msg}."
    elif code == "TOO_MANY_TO_RANK" and rs is not None:
        text = f"{fmt(rs.count)} is too many to rank — narrow to {ctx.cfg.query.max_cards} or fewer first."
    elif code == "UNKNOWN_DOC_ID":
        text = f"No resume “{msg}”."
    elif code == "UNKNOWN_SET":
        text = f"No step “{msg.split(' ', 1)[0]}” in this conversation."
    elif code in ("EMBEDDER_UNAVAILABLE", "LIKE_SOURCE_NOT_FOUND", "LIKE_EMPTY"):
        text = {"EMBEDDER_UNAVAILABLE": "Searching by meaning is not available right now — use a skill or a topic.",
                "LIKE_SOURCE_NOT_FOUND": "That job description is not saved in this conversation — attach it again.",
                "LIKE_EMPTY": "No requirement could be read from that job description."}[code]
    elif code == "RANKING_RUNNING":
        text = "A ranking is running — stop it first, or wait until it is done."
    elif code == "BAD_SORT_KEY":
        text = f"Cannot sort by that — use {', '.join(DEFAULT_DIR)}."
    out = {"role": "error", "code": code, "text": text}
    if fix:
        out["fix"] = fix
    return out


# ---------------------------------------------------------------- the panel


def act(ctx: Context, web: WebSession, action: dict, *, job: dict | None = None, record: bool = True) -> dict:
    """One step from the panel → {state, event?, ui?}."""
    kind = str(action.get("type") or "")
    if job and job.get("running") and kind in CHANGES:
        raise ResumesError("RANKING_RUNNING", "")
    event, ui = None, {}
    if kind in ("page", "next", "prev"):
        goto(ctx, action.get("n") if kind == "page" else kind)
    elif kind in ("filter", "search"):
        step = apply(ctx, web, action.get("args") or {}, new_question=kind == "search", said=action.get("said"))
        event = f"you {step_words(ctx, web, step)} {arrow(ctx, step)}{_kept_stars(ctx, step)}"
    elif kind == "unknown":
        session, rs = verbs._current(ctx)
        f = view.match(view.filters_of(ctx.index, ctx.cfg, session, rs), str(action.get("id")))
        include = bool(action.get("include"))
        step = apply(ctx, web, {**f.args, "include_unknown": include}, replace=f.id, said=f.said)
        what = "years" if f.args.get("min_years") is not None else "rate"
        event = f"you {'included' if include else 'left out'} people with unknown {what} {arrow(ctx, step)}"
    elif kind == "drop":
        step = remove(ctx, web, list(action.get("targets") or []))
        words = step_words(ctx, web, step) or "removed the ranking"
        event = f"you {words} {arrow(ctx, step)}{_kept_stars(ctx, step)}"
    elif kind == "clear":
        step = clear(ctx, web)
        event = f"you cleared all filters {arrow(ctx, step)}"
    elif kind == "sort":
        by = str(action.get("by") or "").strip()
        step = base_order(ctx, web) if by in ("", "base", "relevance", "newest") else sort(ctx, web, by, action.get("judgment"))
        event = f"you sorted by {state.sort_view(step.after, True)['label'].lower()}" if step.after.sort else "you put the list back in its base order"
    elif kind == "rank_off":
        step = remove(ctx, web, ["rank"])
        event = "you removed the ranking"
    elif kind == "rank_on":
        jid = action.get("judgment")
        step = sort(ctx, web, "judgment,rate", jid)
        snap = state.snapshot(ctx, web, with_overview=False)
        crit = (snap.get("ranking") or {}).get("criterion")
        event = f"you turned on the ranking “{crit}”" if crit else "you turned the ranking on"
    elif kind == "undo":
        step = undo(ctx, web)
        event = f"you stepped back {arrow(ctx, step)}"
    elif kind == "use":
        step = use(ctx, web, str(action.get("set")))
        event = f"you went to step {step.after.id.split('_')[1].lstrip('0') or '0'} {arrow(ctx, step)}"
    elif kind == "open":
        p = state.person(ctx, web, str(action.get("id")))
        ui["open"] = p["id"]
        event = f"you opened {'#' + str(p['rank']) + ' · ' if p.get('rank') else ''}{p['title']}"
        action = {**action, "person": p}
    elif kind == "jd":
        text = str(action.get("text") or "")
        if len(text.strip()) < 40:
            raise ResumesError("LIKE_EMPTY", "")
        fname = web.save_jd(text, action.get("name"))
        step = apply(ctx, web, {"like": fname}, new_question=bool(action.get("new")))
        event = f"you {step_words(ctx, web, step)} {arrow(ctx, step)}"
    elif kind == "forget_pending":
        web.set_pending(None)
    else:
        raise ResumesError("BAD_ARGUMENT", f"unknown action {kind!r}")
    out: dict = {"state": state.snapshot(ctx, web, job=job), "ui": ui}
    if event and record:
        out["event"] = web.append({"role": "event", "text": event, "kind": kind, **({"person": action["person"]["id"]} if kind == "open" else {})})
    elif event:
        out["event"] = {"role": "event", "text": event, "kind": kind}
    if kind == "open":
        out["person"] = action["person"]
    return out


# ---------------------------------------------------------------- slash commands


def slash(ctx: Context, web: WebSession, text: str, *, job: dict | None = None) -> dict:
    """→ {state, messages, ui}, or {handoff: words for the assistant}."""
    words = text.strip()[1:].split()
    cmd, rest = (words[0].lower() if words else ""), words[1:]
    arg = " ".join(rest).strip()
    info = None
    ui: dict = {}
    action: dict | None = None
    if cmd in ("next", "prev"):
        action = {"type": cmd}
    elif cmd in ("page", "top"):
        if not arg.isdigit():
            raise ResumesError("BAD_ARGUMENT", f"/{cmd} needs a number")
        if cmd == "page":
            action = {"type": "page", "n": int(arg)}
        else:
            verbs.top(ctx, int(arg))
    elif cmd in ("back", "undo"):
        action = {"type": "undo"}
    elif cmd in ("sets", "history"):
        ui["history"] = True
    elif cmd == "use":
        action = {"type": "use", "set": arg}
    elif cmd == "show":
        if not arg:
            raise ResumesError("BAD_ARGUMENT", "/show needs an id (r002944) or a place in the list (#3)")
        action = {"type": "open", "id": arg}
    elif cmd == "filters":
        snap = state.snapshot(ctx, web, with_overview=False)
        lines = [f"{f['id']} · {chip_text(f)} · {fmt(f['alone'])} alone · without it: {fmt(f['without'])}" for f in snap["filters"]]
        if snap["ranking"]:
            r = snap["ranking"]
            lines.append(f"★ “{r['criterion']}” · {r['judged']} of {fmt(r['total'])} ranked")
        info = "\n".join(lines) if lines else "No filters — the whole collection."
    elif cmd == "drop":
        if not rest:
            raise ResumesError("NO_TARGET", "say what to remove: a filter (f2, or a word of it) or rank")
        action = {"type": "drop", "targets": rest}
    elif cmd in ("clear", "clear-filters"):
        action = {"type": "clear"}
    elif cmd == "sort":
        action = {"type": "sort", "by": arg}
    elif cmd == "rank":
        return {"handoff": f"Rank them for: {arg}" if arg else "Rank them."}
    elif cmd in ("help", "?", ""):
        info = HELP
    else:
        raise ResumesError("UNKNOWN_COMMAND", f"/{cmd} is not a command. {HELP}")
    messages = []
    if action is not None:
        out = act(ctx, web, action, job=job)
        if out.get("event"):
            messages.append(out["event"])
        ui.update(out.get("ui") or {})
        snap = out["state"]
        if action["type"] in ("next", "prev", "page"):
            s = snap["set"]
            messages.append(web.append({"role": "event", "kind": "page", "text": f"page {s['page']} of {fmt(s['pages'])} · {fmt(s['start'])}–{fmt(s['end'])}"}))
    else:
        snap = state.snapshot(ctx, web, job=job)
        if cmd == "top":
            s = snap["set"]
            messages.append(web.append({"role": "event", "kind": "page", "text": f"page {s['page']} of {fmt(s['pages'])} · {fmt(s['start'])}–{fmt(s['end'])}"}))
    if info:
        messages.append(web.append({"role": "info", "text": info}))
    return {"state": snap, "messages": messages, "ui": ui}
