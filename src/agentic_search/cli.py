"""`resumes` command line. Imports are lazy so that cheap verbs start fast."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _cfg():
    from . import config

    return config.load()


def cmd_corpus_build(args: argparse.Namespace) -> int:
    from .ingest.corpus import build

    cfg = _cfg()
    if args.only_changed:
        from .ingest.add import add_new

        ids = add_new(cfg)
        print(json.dumps({"added": ids}))
        return 0
    print(f"corpus build → {cfg.corpus_out}")
    stats = build(cfg, limit=args.limit)
    print(json.dumps(stats, indent=1))
    return 0


def cmd_corpus_stats(args: argparse.Namespace) -> int:
    cfg = _cfg()
    p = cfg.corpus_out / "stats.json"
    if not p.is_file():
        print("CORPUS_NOT_BUILT: run `resumes corpus build`", file=sys.stderr)
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_corpus_show(args: argparse.Namespace) -> int:
    cfg = _cfg()
    p = cfg.corpus_out / "md" / f"{args.id}.md"
    if not p.is_file():
        print(f"UNKNOWN_DOC_ID {args.id}", file=sys.stderr)
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_fixture(args: argparse.Namespace) -> int:
    from .fixture import cli as fixture_cli

    return fixture_cli.run(args, _cfg())


def cmd_index_build(args: argparse.Namespace) -> int:
    from .index.build import build

    cfg = _cfg()
    if args.out:
        cfg.index.out = Path(args.out).resolve()
    if args.only_changed:
        from .index.incremental import add_documents

        stats = add_documents(cfg, extractor=args.extractor)
        print(json.dumps(stats, indent=1))
        return 0
    stats = build(cfg, extractor=args.extractor)
    print(json.dumps(stats, indent=1))
    return 0


def cmd_index_seed(args: argparse.Namespace) -> int:
    from .index import seed

    cfg = _cfg()
    if args.action == "export":
        seed.export(cfg, Path(args.out).resolve(), minus=[Path(m) for m in args.minus])
        return 0
    if args.sources:
        packs = [(s, None) for s in args.sources]
    elif os.environ.get("RESUMES_SEED_FILES"):
        packs = [(s, None) for s in os.environ["RESUMES_SEED_FILES"].split(os.pathsep) if s]
    else:
        listed = json.loads((cfg.root / "data" / "seed.json").read_text(encoding="utf-8"))["packs"]
        local = cfg.root / ".release"  # the same files kept here (checked the same way) are used instead of a download
        packs = [(str(local / name) if (local / (name := p["url"].rsplit("/", 1)[1])).is_file() else p["url"], p["sha256"])
                 for p in listed if args.tests or p["name"] != "tests"]
    for source, sha in packs:
        seed.load(cfg, source, sha)
    return 0


def cmd_index_remove(args: argparse.Namespace) -> int:
    from .index.incremental import remove_document

    try:
        print(json.dumps(remove_document(_cfg(), args.id)))
    except KeyError as e:
        print(str(e).strip("'"), file=sys.stderr)
        return 1
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    import time

    from .index.incremental import watch_once

    cfg = _cfg()
    inbox = [s.path for s in cfg.sources if s.loader == "markdown_dir" and s.id == "inbox"]
    print(f"watching {', '.join(str(p) for p in inbox) or 'the markdown sources'} every {args.interval}s (Ctrl-C to stop)")
    while True:
        r = watch_once(cfg, extractor=args.extractor)
        if r.get("added"):
            print(json.dumps(r))
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_index_stats(args: argparse.Namespace) -> int:
    from .index.store import open_current, read_meta

    cfg = _cfg()
    try:
        con = open_current(cfg.index.out)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    meta = read_meta(con)
    counts = {
        t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("docs", "profile", "terms", "doc_terms", "postings", "member_scores", "chunks", "cards")
    }
    prof = con.execute(
        "SELECT count(*) FILTER (WHERE years IS NOT NULL), count(*) FILTER (WHERE education IS NOT NULL), "
        "count(*) FILTER (WHERE extracted_by IS NOT NULL), count(*) FILTER (WHERE did IS NOT NULL) FROM profile"
    ).fetchone()
    cards = con.execute("SELECT max(tokens), avg(tokens), count(*) FILTER (WHERE tokens > ?) FROM cards", [cfg.index.card_max_tokens]).fetchone()
    print(json.dumps({
        "meta": meta,
        "counts": counts,
        "profile": {"with_years": prof[0], "with_education": prof[1], "llm_extracted": prof[2], "with_did": prof[3]},
        "cards": {"max_tokens": cards[0], "avg_tokens": round(cards[1], 1), "over_cap": cards[2]},
    }, indent=1))
    return 0


def cmd_index_eval(args: argparse.Namespace) -> int:
    from .index.eval_fixture import evaluate

    print(json.dumps(evaluate(_cfg()), indent=1))
    return 0


# ---------------------------------------------------------------- query verbs


def _ctx(args: argparse.Namespace):
    from .query.verbs import Context

    return Context(_cfg(), session_id=getattr(args, "session", None), tty=sys.stdout.isatty())


def _emit(args: argparse.Namespace, out) -> int:
    if getattr(args, "json", False):
        print(json.dumps(out.data, ensure_ascii=False, default=str))
    else:
        print(out.text)
    return 0


def _criteria(args: argparse.Namespace):
    from .query.search import Criteria

    mode: str | int = "all"
    if getattr(args, "min_cover", None):
        mode = int(args.min_cover)
    elif getattr(args, "any", False):
        mode = "any"
    strict = True if getattr(args, "strict", False) else (False if getattr(args, "loose", False) else None)
    return Criteria(
        topics=list(args.topic or []), skills=list(args.skill or []), text=args.text, mode=mode, strict=strict,
        min_years=args.min_years, seniority=[s.strip() for s in (args.seniority or "").split(",") if s.strip()],
        rate_max=args.rate_max, location=args.location, remote=True if args.remote else None,
        availability=[a.strip() for a in (args.availability or "").split(",") if a.strip()],
        level_used=(args.level == "used"), include_unknown=bool(args.include_unknown), like=args.like,
    )


def cmd_search(args):
    from .query import verbs

    return _emit(args, verbs.search(_ctx(args), _criteria(args), show=args.show, page_size=args.page_size, said=args.said))


def cmd_filter(args):
    from .query import verbs

    return _emit(args, verbs.filter_(_ctx(args), args.set, _criteria(args), show=args.show, page_size=args.page_size, said=args.said))


def cmd_drop(args):
    from .query import verbs

    return _emit(args, verbs.drop(_ctx(args), list(args.target), show=args.show, page_size=args.page_size))


def cmd_clear(args):
    from .query import verbs

    return _emit(args, verbs.clear(_ctx(args), show=args.show, page_size=args.page_size))


def cmd_filters(args):
    from .query import verbs

    return _emit(args, verbs.filters_(_ctx(args)))


def cmd_sort(args):
    from .query import verbs

    return _emit(args, verbs.sort_(_ctx(args), args.set, args.by, judgment=args.judgment, show=args.show, page_size=args.page_size))


def cmd_cards(args):
    from .query import verbs

    return _emit(args, verbs.cards(_ctx(args), args.set, new=args.new))


def cmd_score(args):
    from .query import verbs

    return _emit(args, verbs.score(_ctx(args), args.set, source=args.from_, allow_partial=args.allow_partial, criterion=args.criterion,
                                   judge=args.judge, into=args.into))


def cmd_next(args):
    from .query import verbs

    return _emit(args, verbs.next_(_ctx(args), page_size=args.page_size))


def cmd_prev(args):
    from .query import verbs

    return _emit(args, verbs.prev_(_ctx(args)))


def cmd_page(args):
    from .query import verbs

    return _emit(args, verbs.page(_ctx(args), args.n))


def cmd_top(args):
    from .query import verbs

    return _emit(args, verbs.top(_ctx(args), args.n))


def cmd_back(args):
    from .query import verbs

    return _emit(args, verbs.back(_ctx(args)))


def cmd_use(args):
    from .query import verbs

    return _emit(args, verbs.use(_ctx(args), args.set))


def cmd_sets(args):
    from .query import verbs

    return _emit(args, verbs.sets(_ctx(args)))


def cmd_show(args):
    from .query import verbs

    return _emit(args, verbs.show(_ctx(args), args.id, full=args.full, contact=args.contact))


def cmd_vocab(args):
    from .query import verbs

    if args.term == "review":
        return _emit(args, verbs.vocab_review(_ctx(args), top=args.top))
    if args.term in ("add", "alias", "merge"):
        rest = args.rest
        if args.term == "add":
            if len(rest) < 1 or not args.kind or not args.canonical:
                raise SystemExit("usage: resumes vocab add SLUG --kind skill|topic --canonical NAME [--aliases a|b] [--implies s|t] [--description …]")
            return _emit(args, verbs.vocab_edit(_ctx(args), "add", kind=args.kind, slug=rest[0], canonical=args.canonical,
                                                aliases=[a for a in (args.aliases or "").split("|") if a], implies=[i for i in (args.implies or "").split("|") if i],
                                                description=args.description or ""))
        if args.term == "alias":
            if len(rest) != 2:
                raise SystemExit("usage: resumes vocab alias SLUG FORM")
            return _emit(args, verbs.vocab_edit(_ctx(args), "alias", slug=rest[0], form=rest[1]))
        if len(rest) != 2:
            raise SystemExit("usage: resumes vocab merge FROM INTO")
        return _emit(args, verbs.vocab_edit(_ctx(args), "merge", src=rest[0], dst=rest[1]))
    return _emit(args, verbs.vocab(_ctx(args), args.term))


def cmd_union(args):
    from .query import verbs

    return _emit(args, verbs.union(_ctx(args), args.a, args.b))


def cmd_minus(args):
    from .query import verbs

    return _emit(args, verbs.minus(_ctx(args), args.a, args.b))


def cmd_judge(args):
    from .query import verbs

    return _emit(args, verbs.judge(_ctx(args), args.set, criterion=args.criterion, model=args.backend.split(":", 1)[-1], batch=args.batch,
                                   log=lambda m: print(m, file=sys.stderr)))


def cmd_session(args):
    from .query import verbs

    ctx = _ctx(args)
    if args.sub == "new":
        return _emit(args, verbs.session_new(ctx, args.id))
    if args.sub == "use":
        return _emit(args, verbs.session_use(ctx, args.id))
    if args.sub == "gc":
        return _emit(args, verbs.session_gc(ctx, args.days))
    return _emit(args, verbs.session_list(ctx))


def cmd_mcp(args):
    try:
        from .mcp import serve
    except ModuleNotFoundError as e:
        print(f"MCP_UNAVAILABLE {e.name}: install with `uv sync --extra mcp` (or `uv tool install --editable '.[mcp]'`)", file=sys.stderr)
        return 1
    return serve(_cfg(), session_id=args.session)


def cmd_serve(args):
    from pathlib import Path as _P

    from .serve import serve

    return serve(_cfg(), embedder_spec=args.embedder, path=_P(args.socket) if args.socket else None)


def cmd_web(args):
    try:
        from .web.launch import serve
    except ModuleNotFoundError as e:
        print(f"WEB_UNAVAILABLE {e.name}: install with `uv sync --extra web` (or `uv tool install --editable '.[mcp,web]'`)", file=sys.stderr)
        return 1
    return serve(_cfg(), host=args.host, port=args.port, open_browser=args.open, public_host=args.public_host, signup=args.signup)


def cmd_users(args):
    """The accounts of `resumes web`, kept in [web].state_dir/users.json."""
    from .web import users

    path = _cfg().web.state_dir / users.FILE
    if args.sub == "list":
        rows = users.records(path)
        for name, rec in rows:
            print(f"{name}\t{(rec.get('created') or '')[:19]}\t{users.role_of(rec)}\t{rec.get('phone') or '-'}\t{rec.get('via') or 'operator'}")
        print(f"{len(rows)} user{'s' if len(rows) != 1 else ''} in {path} · login is {'on' if rows else 'off'}", file=sys.stderr)
        return 0
    if args.sub == "usage":
        rows = users.usage(path.with_name(users.USAGE))
        roles = {n: users.role_of(r) for n, r in users.records(path)}
        print("name\trole\tturns 24h\tcost 24h\tturns 30d\tcost 30d")
        for name in sorted(rows, key=lambda n: -rows[n]["month_cost"]):
            u = rows[name]
            print(f"{name}\t{roles.get(name, '-')}\t{u['day_turns']}\t${u['day_cost']:.4f}\t{u['month_turns']}\t${u['month_cost']:.4f}")
        total = sum(u["month_cost"] for u in rows.values())
        print(f"{len(rows)} {'person' if len(rows) == 1 else 'people'} used the assistant in 30 days · ${total:.4f} in all", file=sys.stderr)
        return 0
    if args.sub == "remove":
        if not users.remove(path, args.name):
            print(f"UNKNOWN_USER {args.name}", file=sys.stderr)
            return 1
        left = len(users.names(path))
        print(f"removed {users.normalize_name(args.name)} · {left} user{'s' if left != 1 else ''} left · login is {'on' if left else 'off'}", file=sys.stderr)
        return 0
    if args.sub == "block":
        if not users.block(path, args.name):
            print(f"UNKNOWN_USER {args.name}", file=sys.stderr)
            return 1
        print(f"blocked {users.normalize_name(args.name)} · out at once; neither the email nor its numbers pass the check again (`users add NAME --member` lets it back in)", file=sys.stderr)
        return 0
    name = users.normalize_name(args.name)
    password = args.password
    if password is None:
        import getpass

        password = getpass.getpass(f"password for {name}: ")
        if password != getpass.getpass("again: "):
            print("the two passwords differ", file=sys.stderr)
            return 1
    role = "member" if args.member else "admin" if args.admin else None
    key, new = users.add(path, name, password, role=role)
    n = len(users.names(path))
    rec = dict(users.records(path)).get(key, {})
    print(f"{'added' if new else 'password changed for'} {key} ({users.role_of(rec)}) · {n} user{'s' if n != 1 else ''} · login is on for resumes web (in force at once)", file=sys.stderr)
    return 0


def cmd_engine(args):
    try:
        from .web.app import serve_engine
    except ModuleNotFoundError as e:
        print(f"WEB_UNAVAILABLE {e.name}: install with `uv sync --extra web` (or `uv tool install --editable '.[mcp,web]'`)", file=sys.stderr)
        return 1
    cfg = _cfg()
    if args.sessions:
        from pathlib import Path as _P

        cfg.query.sessions = _P(args.sessions).resolve()
    return serve_engine(cfg, host=args.host, port=args.port)


def cmd_bench(args):
    from .query.bench import run

    return run(_cfg(), runs=args.runs, as_json=args.json)


def _query_parsers(sub) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--session", default=None, metavar="SID", help="session id (default: $RESUMES_SESSION, then CURRENT_SESSION)")

    crit = argparse.ArgumentParser(add_help=False)
    crit.add_argument("--topic", action="append", metavar="T", help="a topic in the user's words (repeatable)")
    crit.add_argument("--skill", action="append", metavar="S", help="a skill (repeatable)")
    crit.add_argument("--text", default=None, metavar="TEXT", help="free text: FTS + embedding scan (needs an embedder)")
    crit.add_argument("--like", default=None, metavar="FILE|ID", help="a job description file (bare name: in the session dir) or a document id; decides membership when no --topic/--skill/--text, otherwise only ranks")
    mode = crit.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="every term must match (default)")
    mode.add_argument("--any", action="store_true", help="any term may match; ranked by coverage")
    mode.add_argument("--min-cover", type=int, default=None, metavar="K", help="at least K of the terms")
    ev = crit.add_mutually_exclusive_group()
    ev.add_argument("--strict", action="store_true", help="strong evidence only, for every term")
    ev.add_argument("--loose", action="store_true", help="include semantic (weak) matches for every term")
    crit.add_argument("--min-years", type=float, default=None, metavar="N")
    crit.add_argument("--seniority", default=None, metavar="S[,S]", help="junior, mid, senior, lead, principal, manager, director, executive")
    crit.add_argument("--rate-max", type=float, default=None, metavar="X")
    crit.add_argument("--location", action="append", default=None, metavar="L", help="substring of the location (\"Krakow, Poland\"); repeat for any of several places")
    crit.add_argument("--remote", action="store_true", help="remote ok only")
    crit.add_argument("--availability", default=None, metavar="A[,A]", help="now, 2w, 1m, 3m (as the `avail` facet shows); people who state none do not match")
    crit.add_argument("--level", choices=["used"], default=None, help="used: evidence in a job or project, not a skills list")
    crit.add_argument("--include-unknown", action="store_true", help="keep documents whose years/rate are unknown under --min-years/--rate-max")
    crit.add_argument("--said", default=None, metavar="WORDS", help="the user's own words for this condition (shown by `resumes filters`)")

    paging = argparse.ArgumentParser(add_help=False)
    paging.add_argument("--show", action=argparse.BooleanOptionalAction, default=None, help="print page 1 (or not) whatever the page-printing rule says")
    paging.add_argument("--page-size", type=int, default=None, metavar="N")

    s = sub.add_parser("search", parents=[common, crit, paging], help="a new question: topics/skills/text + constraints become the filters; prints count + facets")
    s.set_defaults(func=cmd_search)
    f = sub.add_parser("filter", parents=[common, crit, paging], help="add filters to the current view (order and judgments carry over)")
    f.add_argument("set", nargs="?", default=None, help="rs_NN (default: current)")
    f.set_defaults(func=cmd_filter)
    d = sub.add_parser("drop", parents=[common, paging], help="remove filters from the current view: `drop f2`, `drop elixir`; `drop rank` removes the ranking")
    d.add_argument("target", nargs="+", help="a filter id (f2), a word of the filter (elixir, rate), or `rank`")
    d.set_defaults(func=cmd_drop)
    sub.add_parser("clear", parents=[common, paging], help="remove every filter and the ranking: all CVs").set_defaults(func=cmd_clear)
    sub.add_parser("filters", parents=[common], help="the active filters, what each does to the count, and the ranking").set_defaults(func=cmd_filters)
    so = sub.add_parser("sort", parents=[common, paging], help="new set with the same members in a new order; prints page 1")
    so.add_argument("set", nargs="?", default=None)
    so.add_argument("--by", required=True, metavar="KEY[,KEY]", help="judgment, rate, years, relevance, seniority (optionally key:asc|desc)")
    so.add_argument("--judgment", default=None, metavar="j_NN", help="sort by an older judgment")
    so.set_defaults(func=cmd_sort)
    c = sub.add_parser("cards", parents=[common], help="the cards to judge (refuses above [query].max_cards people); with a ranking, only the people not judged yet")
    c.add_argument("set", nargs="?", default=None)
    c.add_argument("--new", action="store_true", help="all cards, for a criterion other than the current ranking's")
    c.set_defaults(func=cmd_cards)
    sc = sub.add_parser("score", parents=[common], help="record the agent's scores as a judgment on the set")
    sc.add_argument("set", nargs="?", default=None)
    sc.add_argument("--from", dest="from_", default="scores.json", metavar="FILE|-", help="scores file (bare name: in the session dir); - for stdin")
    sc.add_argument("--into", default=None, metavar="j_NN", help="add the scores to this judgment (same criterion); the file's \"judgment\" field does the same")
    sc.add_argument("--allow-partial", action="store_true", help="missing ids get a null score and sort last")
    sc.add_argument("--criterion", default=None, help="override the file's criterion")
    sc.add_argument("--judge", default=None, help="override the file's judge")
    sc.set_defaults(func=cmd_score)
    n = sub.add_parser("next", parents=[common], help="next page of the current set")
    n.add_argument("--page-size", type=int, default=None, metavar="N")
    n.set_defaults(func=cmd_next)
    sub.add_parser("prev", parents=[common], help="previous page of the current set").set_defaults(func=cmd_prev)
    pg = sub.add_parser("page", parents=[common], help="page N of the current set")
    pg.add_argument("n", type=int)
    pg.set_defaults(func=cmd_page)
    tp = sub.add_parser("top", parents=[common], help="first N of the current set")
    tp.add_argument("n", type=int)
    tp.set_defaults(func=cmd_top)
    sub.add_parser("back", parents=[common], help="make the parent set current and show its page").set_defaults(func=cmd_back)
    u = sub.add_parser("use", parents=[common], help="make rs_NN current")
    u.add_argument("set")
    u.set_defaults(func=cmd_use)
    sub.add_parser("sets", parents=[common], help="the session's sets as a lineage tree").set_defaults(func=cmd_sets)
    sh = sub.add_parser("show", parents=[common], help="one document: card (default) or --full markdown")
    sh.add_argument("id", help="r000412 or 412")
    sh.add_argument("--full", action="store_true")
    sh.add_argument("--contact", action="store_true", help="unredacted source (needs allow_contact = true)")
    sh.set_defaults(func=cmd_show)
    v = sub.add_parser("vocab", parents=[common], help="how a word resolves; or review | add SLUG | alias SLUG FORM | merge FROM INTO (edits schema/vocab.csv)")
    v.add_argument("term", help="a word, or review/add/alias/merge")
    v.add_argument("rest", nargs="*")
    v.add_argument("--top", type=int, default=30, help="review: how many unresolved names")
    v.add_argument("--kind", choices=["skill", "topic"], default=None)
    v.add_argument("--canonical", default=None)
    v.add_argument("--aliases", default=None, help="pipe-separated")
    v.add_argument("--implies", default=None, help="pipe-separated slugs")
    v.add_argument("--description", default=None)
    v.set_defaults(func=cmd_vocab)
    un = sub.add_parser("union", parents=[common], help="new set: A ∪ B (A's order first)")
    un.add_argument("a")
    un.add_argument("b")
    un.set_defaults(func=cmd_union)
    mi = sub.add_parser("minus", parents=[common], help="new set: A − B (A's order kept)")
    mi.add_argument("a")
    mi.add_argument("b")
    mi.set_defaults(func=cmd_minus)
    jd = sub.add_parser("judge", parents=[common], help="server-side judge (OpenAI) for batch/eval use: records a judgment like `score`")
    jd.add_argument("set", nargs="?", default=None)
    jd.add_argument("--criterion", required=True)
    jd.add_argument("--backend", default="openai:gpt-5-mini")
    jd.add_argument("--batch", type=int, default=25)
    jd.set_defaults(func=cmd_judge)
    se = sub.add_parser("session", parents=[common], help="sessions: new | use ID | list | gc")
    ssub = se.add_subparsers(dest="sub", required=True)
    sn = ssub.add_parser("new", parents=[common])
    sn.add_argument("id", nargs="?", default=None)
    su = ssub.add_parser("use", parents=[common])
    su.add_argument("id")
    ssub.add_parser("list", parents=[common])
    sg = ssub.add_parser("gc", parents=[common])
    sg.add_argument("--days", type=int, default=None)
    se.set_defaults(func=cmd_session)
    m = sub.add_parser("mcp", help="MCP server over stdio exposing the same verbs as tools (one process = one session)")
    m.add_argument("--session", default=None, metavar="SID", help="bind to this session (default: $RESUMES_SESSION, else a new mcp-<hex> session)")
    m.set_defaults(func=cmd_mcp)
    sv = sub.add_parser("serve", help="keep the embedder warm behind a Unix socket; --text/--like use it when present")
    sv.add_argument("--embedder", default=None, metavar="SPEC", help="default: the index's embedder (e.g. openai:text-embedding-3-large, local:BAAI/bge-m3)")
    sv.add_argument("--socket", default=None, metavar="PATH")
    sv.set_defaults(func=cmd_serve)
    wb = sub.add_parser("web", help="the chat + results UI and the HTTP API on localhost")
    wb.add_argument("--host", default=None, metavar="HOST", help="default: [web].host (127.0.0.1)")
    wb.add_argument("--port", type=int, default=None, metavar="PORT", help="default: [web].port (8765)")
    wb.add_argument("--open", action="store_true", help="open the browser")
    wb.add_argument("--public-host", default=None, metavar="NAME[,NAME]", help="behind a proxy (Caddy): the public name(s) accepted in Host and Origin; default: [web].public_host")
    wb.add_argument("--signup", action="store_true", default=None, help="the email and phone check before anyone may search (an email code every time, an SMS code for a number not seen before); needs RESEND_* and SAKARI_* in the environment; default: [signup].enabled")
    wb.set_defaults(func=cmd_web)
    us = sub.add_parser("users", help="the accounts of `resumes web`: login is on as soon as there is one")
    usub = us.add_subparsers(dest="sub", required=True)
    ua = usub.add_parser("add", help="make an account, or change its password; asks for the password twice")
    ua.add_argument("name", help="the account name, e.g. an email address")
    ua.add_argument("--password", default=None, metavar="PW", help="instead of the prompt (scripts, tests)")
    uar = ua.add_mutually_exclusive_group()
    uar.add_argument("--member", action="store_true", help="a member: the limits of [limits] apply (default for a new account: admin; a known one keeps its role)")
    uar.add_argument("--admin", action="store_true", help="an admin: no limits, any model")
    ua.set_defaults(func=cmd_users)
    usub.add_parser("list", help="the accounts, one per line: name, created, role, phone (the last one), how it was made (operator, signup, check)").set_defaults(func=cmd_users)
    ur = usub.add_parser("remove", help="delete an account; the person is out at once (with the check on: in again by passing it)")
    ur.add_argument("name")
    ur.set_defaults(func=cmd_users)
    ub = usub.add_parser("block", help="block an account: out at once, and neither its email nor its numbers pass the check again")
    ub.add_argument("name")
    ub.set_defaults(func=cmd_users)
    usub.add_parser("usage", help="each person's assistant turns and model cost, the last 24 h and 30 days (.resumes/usage.jsonl)").set_defaults(func=cmd_users)

    en = sub.add_parser("engine", help="the engine alone, behind its HTTP API on 127.0.0.1; `resumes web` starts it for you")
    en.add_argument("--host", default=None, metavar="HOST", help="default: 127.0.0.1")
    en.add_argument("--port", type=int, default=None, metavar="PORT", help="default: [web].engine_port (8770)")
    en.add_argument("--sessions", default=None, metavar="DIR", help="where the conversations live (tests); default: [query].sessions")
    en.set_defaults(func=cmd_engine)
    b = sub.add_parser("bench", parents=[common], help="replay the golden scenario; p50/p95 per verb against its time targets")
    b.add_argument("--runs", type=int, default=5)
    b.set_defaults(func=cmd_bench)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="resumes", description="conversational resume search for coding agents")
    sub = p.add_subparsers(dest="cmd", required=True)
    _query_parsers(sub)

    corpus = sub.add_parser("corpus", help="build and inspect the canonical markdown corpus")
    csub = corpus.add_subparsers(dest="sub", required=True)
    b = csub.add_parser("build", help="sources → corpus/md/*.md")
    b.add_argument("--limit", type=int, default=None, help="stop after N documents (smoke runs)")
    b.add_argument("--only-changed", action="store_true", help="convert only inbox files the registry has not seen")
    b.set_defaults(func=cmd_corpus_build)
    csub.add_parser("stats", help="print corpus/stats.json").set_defaults(func=cmd_corpus_stats)
    s = csub.add_parser("show", help="print one document")
    s.add_argument("id")
    s.set_defaults(func=cmd_corpus_show)

    fixture = sub.add_parser("fixture", help="synthetic fixture corpus (data/fixture)")
    fsub = fixture.add_subparsers(dest="sub", required=True)
    fsub.add_parser("personas", help="write data/fixture/personas.jsonl + ground_truth.json from SPEC").set_defaults(func=cmd_fixture)
    g = fsub.add_parser("generate", help="write resumes for personas that have none yet (OpenAI)")
    g.add_argument("--limit", type=int, default=None)
    g.add_argument("--concurrency", type=int, default=8)
    g.add_argument("--model", default=None)
    g.set_defaults(func=cmd_fixture)
    fsub.add_parser("validate", help="check every fixture resume against its persona").set_defaults(func=cmd_fixture)

    index = sub.add_parser("index", help="build and inspect the DuckDB index")
    isub = index.add_subparsers(dest="sub", required=True)
    ib = isub.add_parser("build", help="corpus/md → index/resumes-<version>.duckdb (atomic swap of index/current)")
    ib.add_argument("--extractor", default=None, help="override [index].extractor, e.g. none")
    ib.add_argument("--out", default=None, metavar="DIR", help="build into another directory (query it with RESUMES_INDEX_DIR=DIR)")
    ib.add_argument("--only-changed", action="store_true", help="append documents that are in corpus/md but not in the index (seconds, not a rebuild)")
    ib.set_defaults(func=cmd_index_build)
    ir = isub.add_parser("remove", help="mark a document deleted and drop it from every bitmap")
    ir.add_argument("id")
    ir.set_defaults(func=cmd_index_remove)
    w = sub.add_parser("watch", help="poll the inbox; convert and add new files (corpus + index --only-changed)")
    w.add_argument("--interval", type=int, default=30)
    w.add_argument("--extractor", default=None)
    w.add_argument("--once", action="store_true")
    w.set_defaults(func=cmd_watch)
    isub.add_parser("stats", help="counts and metadata of the current index").set_defaults(func=cmd_index_stats)
    sd = isub.add_parser("seed", help="the model's saved outputs as one file: build the index without a key")
    sdsub = sd.add_subparsers(dest="action", required=True)
    sl = sdsub.add_parser("load", help="put seed packs into index/cache/ (default: the packs of data/seed.json)")
    sl.add_argument("sources", nargs="*", metavar="FILE|URL")
    sl.add_argument("--tests", action="store_true", help="also the pack the tests' index needs")
    sl.set_defaults(func=cmd_index_seed)
    se = sdsub.add_parser("export", help="build once more from the cache and pack exactly what the build read")
    se.add_argument("out", metavar="FILE")
    se.add_argument("--minus", action="append", default=[], metavar="FILE", help="leave out what this pack already holds")
    se.set_defaults(func=cmd_index_seed)
    isub.add_parser("eval-fixture", help="membership precision/recall against data/fixture/ground_truth.json").set_defaults(func=cmd_index_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    from .errors import ResumesError

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ResumesError as e:
        print(f"{e.code} {e.message}".strip(), file=sys.stderr)
        if getattr(args, "json", False):
            print(json.dumps({"error": e.code, "message": e.message, **({"data": e.data} if e.data else {})}, ensure_ascii=False))
        return 1
    except FileNotFoundError as e:  # not built yet, or run outside the project: the message says which
        print(str(e), file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
