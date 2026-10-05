"""Text the agent reads: compact, one state line at the end, one-line errors.

Links are file:// URLs (or `link_template`), wrapped in OSC 8 hyperlinks when stdout is a terminal.
"""

from __future__ import annotations

from ..index.cards import _title_case
from ..session.store import ResultSet
from .index import Profile
from .search import Membership, describe_args
from .terms import expansion

SYMBOL = {"EUR": "€", "USD": "$", "GBP": "£"}
EXPANSION_SHOWN = 6
SEP = " · "


def rate_symbol(currency: str | None) -> str:
    return SYMBOL.get(currency or "", (currency or "") + " ")


def fmt_rate(v: float | None, currency: str | None) -> str:
    return "rate ?" if v is None else f"{rate_symbol(currency)}{v:.0f}/h"


def fmt_years(y: float | None) -> str:
    if y is None:
        return "?y"
    return f"{y:.0f}y" if y >= 2 else f"{y:.1f}y"


def osc8(url: str, tty: bool) -> str:
    return f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\" if tty else url


def star(score: int | None, note: str) -> str:
    s = "★?" if score is None else f"★{score}"
    return f"{s} {note}".rstrip()


# ---------------------------------------------------------------- headers and state


def term_summary(m: Membership, vocab, mode: str | int) -> str:
    """`topic data-pipelines (expanded: etl, airflow, +11) AND skill elixir (incl. phoenix-framework, ecto)`."""
    parts = []
    for r in m.resolved:
        exp = expansion(vocab, r.slug)
        label = f"{r.kind} {r.slug}"
        if exp:
            shown = ", ".join(exp[:EXPANSION_SHOWN]) + (f", +{len(exp) - EXPANSION_SHOWN}" if len(exp) > EXPANSION_SHOWN else "")
            label += f" ({'expanded' if r.kind == 'topic' else 'incl.'}: {shown})"
        parts.append(label)
    if m.text is not None:
        parts.append(f'text "{m.text.query}" (words: {" ".join(m.text.words) or "—"}; cosine ≥ {m.text.tau:g})')
    if m.like is not None and not parts:
        return m.like.summary + f" · cover ≥ {m.like.min_cover:.0%}"
    if m.like is not None:
        parts[-1] += f" · ranked by {m.like.summary}"
    if not parts:
        return "all documents"
    if mode == "all":
        return " AND ".join(parts)
    if mode == "any":
        return " OR ".join(parts)
    return f"≥{mode} of: " + ", ".join(parts)


def resolution_lines(m: Membership) -> list[str]:
    lines = []
    echo = [r.echo for r in m.resolved if r.given.strip().lower() not in (r.slug, r.canonical.lower())]
    if echo:
        lines.append("resolved  " + SEP.join(echo))
    if m.unresolved:
        lines.append(f'no topic/skill matched "{" ".join(m.unresolved)}" — text search')
    return lines


def constraint_summary(args: dict) -> str:
    keys = {k: v for k, v in args.items() if k in ("min_years", "rate_max", "location", "remote", "seniority", "availability", "level", "strict")}
    return describe_args(keys)


def unknown_notice(m: Membership, include_unknown: bool) -> str:
    bits = []
    if m.unknown_years:
        bits.append(f"+{m.unknown_years} with unknown years")
    if m.unknown_rate:
        bits.append(f"+{m.unknown_rate} with unknown rate")
    if not bits:
        return ""
    return SEP.join(bits) + ("" if include_unknown else " (--include-unknown)")


def people(n: int) -> str:
    return "1 person" if n == 1 else f"{n} people"


def count_header(rs: ResultSet, m: Membership, vocab, include_unknown: bool) -> str:
    terms = term_summary(m, vocab, rs.args.get('mode', 'all'))
    cons = constraint_summary(rs.args)
    head = f"{rs.id}{SEP}{people(rs.count)}"
    if not (cons and terms == "all documents"):          # a constraint alone (`filter --rate-max 80`) is not "all documents"
        head += SEP + terms
    if cons:
        head += SEP + cons
    un = unknown_notice(m, include_unknown)
    if un:
        head += SEP + un
    return head


def facet_lines(f: dict) -> list[str]:
    if f.get("count", 0) == 0:
        return ["no members"]
    lines = []
    if "strong" in f and f.get("strong") != f.get("count"):
        lines.append(f"evidence  strong {f['strong']}{SEP}weak {f['weak']}")
    if f.get("seniority"):
        lines.append("seniority " + SEP.join(f"{s} {n}" for s, n in f["seniority"]))
    y = f.get("years") or {}
    if y.get("p50") is not None:
        line = f"years     p25 {y['p25']}{SEP}p50 {y['p50']}{SEP}p75 {y['p75']}"
        if y.get("unknown"):
            line += f"{SEP}unknown {y['unknown']}"
        lines.append(line)
    r = f.get("rate") or {}
    if r.get("p50") is not None:
        line = f"rate {rate_symbol(r.get('currency'))}/h  p25 {r['p25']}{SEP}p50 {r['p50']}{SEP}p75 {r['p75']}"
        if r.get("synthetic"):
            line += f"   (synthetic rate for {r['synthetic']} of {f['count']})"
        lines.append(line)
    if f.get("skills"):
        lines.append("skills    " + SEP.join(f"{s} {n}" for s, n in f["skills"]))
    w = f.get("where") or {}
    if w.get("remote_ok") or w.get("top"):
        bits = ([f"remote-ok {w['remote_ok']}"] if w.get("remote_ok") else []) + [f"{p} {n}" for p, n in w.get("top", [])]
        lines.append("where     " + SEP.join(bits))
    if f.get("availability"):
        lines.append("avail     " + SEP.join(f"{a} {n}" for a, n in f["availability"]))
    return lines


def next_hint(rs: ResultSet, max_cards: int | None = None) -> str:
    if max_cards is not None and rs.count > max_cards:
        return f"next: `resumes next` to list{SEP}`resumes filter --skill …` to narrow (ranking needs ≤ {max_cards} people; this set has {rs.count})"
    return f"next: `resumes cards {rs.id}` to rank{SEP}`resumes next` to list{SEP}`resumes filter {rs.id} --skill …` to narrow"


def sort_label(rs: ResultSet) -> str:
    if not rs.sort:
        return ""
    return "sorted " + " ".join(k.replace(":desc", "↓").replace(":asc", "↑") for k in rs.sort)


def origin_label(rs: ResultSet) -> str:
    desc = describe_args(rs.args)
    op = rs.op if not desc else f"{rs.op} {desc}"
    return f"from {rs.parent} ({op})" if rs.parent else f"from {rs.op} ({desc})" if desc else f"from {rs.op}"


def state_line(rs: ResultSet, *, index_changed: bool = False, end: bool = False,
               filters: list[tuple[str, str]] | None = None, judged: tuple[str, int] | None = None) -> str:
    """`— rs_05 · 15 · page 1/2 · f1 topic=data-pipelines · f2 skill=elixir · sorted judgment↓ rate↑ (j_01 15/15)`.

    `filters` is None for an old-format set; `judged` is (judgment, how many of the set it covers)."""
    parts = [rs.id, str(rs.count), f"page {rs.page_no}/{rs.pages}"]
    if end:
        parts.append("end of set")
    if filters is not None:
        parts += [f"{fid} {label}" for fid, label in filters] or ["no filters (all CVs)"]
    s = sort_label(rs)
    cover = f"{judged[0]} {judged[1]}/{rs.count}" if judged else ""
    if s:
        parts.append(s + (f" ({cover})" if cover and "judgment" in s else ""))
    if cover and "judgment" not in s:
        parts.append(f"{cover} judged")
    if filters is None:
        parts.append(origin_label(rs))
    if index_changed:
        parts.append("(index updated since this set was made)")
    return "— " + SEP.join(parts)


# ---------------------------------------------------------------- cards and links


def card_text(card: str, judged: tuple[int | None, str] | None, versions: int = 0, *, link: str | None = None) -> str:
    """A stored card, annotated at render time; `link`, if given, becomes its last line."""
    lines = [ln for ln in card.split("\n") if not ln.startswith("→ ")]
    if versions:
        lines[0] += f" (+{versions} other version{'s' if versions > 1 else ''})"
    if judged is not None:
        lines[0] += " " + star(*judged)
    if link:
        lines.append(f"→ {link}")
    return "\n".join(lines)


def link_line(rank: int, p: Profile, link: str, judged: tuple[int | None, str] | None, tty: bool, versions: int = 0) -> str:
    parts = [f"{rank}. {_title_case(p.title or 'Resume')[:60]}", fmt_years(p.years), fmt_rate(p.rate, p.currency)]
    if judged is not None:
        parts.append(star(*judged))
    if versions:
        parts.append(f"+{versions} version{'s' if versions > 1 else ''}")
    parts.append(osc8(link, tty))
    return SEP.join(parts)


def page_header(rs: ResultSet, start: int, end: int) -> str:
    """`rs_03 · 25 people · 11–20 of 25 · sorted judgment↓ rate↑`."""
    parts = [rs.id, people(rs.count), f"{start + 1}–{end} of {rs.count}"]
    s = sort_label(rs)
    if s:
        parts.append(s)
    return SEP.join(parts)


def page_text(header: str, cards: list[str], links: list[str], footer: str) -> str:
    out = [header, "cards:"]
    out.append("\n\n".join(cards))
    out.append("links:")
    out.extend(links)
    out.append(footer)
    return "\n".join(out)


def error_line(code: str, message: str) -> str:
    return f"{code} {message}".strip()
