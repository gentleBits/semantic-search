"""Cards: a person in at most 110 tokens, what an agent reads when it ranks.

    r000412 · Senior Data Engineer · 9y · €68/h · Berlin (remote ok) · avail 2w
    skills: Python, Spark, Airflow, Kafka, dbt, Elixir, Postgres · listed only: AWS, Scala
    did: built Kafka→Snowflake CDC pipeline (2B events/day); led 4-person platform team

The link to the resume is not stored: it depends on the machine, so it is added when a card is shown.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

LEVEL_ORDER = {"led": 0, "used": 1, "listed": 2}


@dataclass
class CardInput:
    id: str
    title: str
    years: float | None
    rate: int | None
    currency: str
    location: str | None
    remote: bool | None
    availability: str | None
    skills: list[tuple[str, str]]     # (canonical, level) — skills only, no topics
    did: str | None


def _title_case(s: str) -> str:
    return s.title() if s.isupper() else s


def _availability(a: str | None) -> str | None:
    if not a:
        return None
    a = a.strip().lower()
    m = re.match(r"(\d+)\s*(week|month|day)s?", a)
    if m:
        return f"avail {m.group(1)}{m.group(2)[0]}"
    if a.startswith("immediate"):
        return "avail now"
    return f"avail {a}"[:20]


def _line1(c: CardInput) -> str:
    parts = [c.id, _title_case(c.title)[:60]]
    if c.years is not None:
        parts.append(f"{c.years:.0f}y" if c.years >= 2 else f"{c.years:.1f}y")
    if c.rate is not None:
        sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(c.currency, c.currency + " ")
        parts.append(f"{sym}{c.rate}/h")
    if c.location:
        loc = c.location.split(",")[0].strip()
        parts.append(f"{loc} (remote ok)" if c.remote else loc)
    elif c.remote:
        parts.append("remote ok")
    if (av := _availability(c.availability)):
        parts.append(av)
    return " · ".join(parts)


def render(c: CardInput, count: Callable[[str], int], max_tokens: int = 110) -> str:
    used = [n for n, lvl in sorted(c.skills, key=lambda x: LEVEL_ORDER.get(x[1], 3)) if lvl in ("led", "used")]
    listed = [n for n, lvl in c.skills if lvl == "listed"]
    did = (c.did or "").strip().rstrip(".")
    n_used, n_listed = len(used), len(listed)
    while True:
        lines = [_line1(c)]
        skills = ", ".join(used[:n_used])
        if listed[:n_listed]:
            skills = (skills + " · " if skills else "") + "listed only: " + ", ".join(listed[:n_listed])
        if skills:
            lines.append(f"skills: {skills}")
        if did:
            lines.append(f"did: {did}")
        text = "\n".join(lines)
        if count(text) <= max_tokens:
            return text
        # trim: listed first, then used, then the did line
        if n_listed > 0:
            n_listed -= 1
        elif n_used > 4:
            n_used -= 1
        elif len(did) > 41:
            did = did[: max(40, len(did) - 21)].rstrip() + "…"
        elif n_used > 0:
            n_used -= 1
        else:
            return text  # nothing left to trim; the caller records the overshoot
