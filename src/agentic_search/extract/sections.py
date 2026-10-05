"""Parse a canonical resume markdown into headline, sections and entries.

Layout produced by the ingest converters (all three sources):
    # headline
    ## Section title
    ### Entry title                       (job title, degree, or company)
    *Company — City, State · Mon YYYY to Mon YYYY*   (meta line: any part may be missing)
    - bullets / paragraphs
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

KIND_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("education", re.compile(r"education|academic|degree|coursework|training and|schooling", re.I)),
    ("experience", re.compile(r"experience|employment|work history|professional background|career|positions|projects|accomplishments|achievements|internship|history", re.I)),
    ("skills", re.compile(r"skill|highlight|qualification|competenc|expertise|technical|technolog|tools|proficien|strength|certification|licens|languages|software", re.I)),
    ("summary", re.compile(r"summary|profile|objective|about|overview|focus|introduction", re.I)),
    ("skip", re.compile(r"interest|hobbies|personal|references|affiliation|additional|activities|awards|honors|military|volunteer|publications|presentations", re.I)),
]

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_RE = re.compile(
    rf"(?P<my>{_MONTH_RE}\s+\d{{4}})|(?P<ym>\d{{4}}-\d{{1,2}})|(?P<mdy>\d{{1,2}}/\d{{1,2}}/\d{{2,4}})|(?P<mony>\d{{1,2}}/\d{{4}})|(?P<y>(?<!\d)(?:19|20)\d{{2}}(?!\d))",
    re.I,
)
_CURRENT_RE = re.compile(r"\b(current|present|now|to date|today|ongoing)\b", re.I)
_SEP_RE = re.compile(r"\s*(?:to|-|–|—|through|until|till)\s*", re.I)
LED_RE = re.compile(r"^\W*(led|lead|managed|owned|architected|directed|headed|spearheaded|drove|oversaw|founded|established|supervised|coordinated a team|built and led|mentored)\b", re.I)


def _parse_date(s: str, end: bool = False) -> tuple[int, int] | None:
    s = s.strip().lower().replace("sept", "sep")
    m = re.match(rf"({_MONTH_RE})\s+(\d{{4}})$", s)
    if m:
        return int(m.group(2)), _MONTHS[m.group(1)[:3]]
    m = re.match(r"(\d{4})-(\d{1,2})$", s)
    if m:
        return int(m.group(1)), max(1, min(12, int(m.group(2))))
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)
    if m:
        y = int(m.group(3))
        y = y + (2000 if y < 50 else 1900) if y < 100 else y
        return y, max(1, min(12, int(m.group(1))))
    m = re.match(r"(\d{1,2})/(\d{4})$", s)
    if m:
        return int(m.group(2)), max(1, min(12, int(m.group(1))))
    m = re.match(r"((?:19|20)\d{2})$", s)
    if m:
        return int(m.group(1)), 12 if end else 1
    return None


@dataclass
class DateRange:
    start: tuple[int, int]
    end: tuple[int, int]
    open_ended: bool = False

    def months(self) -> int:
        return max(0, (self.end[0] - self.start[0]) * 12 + (self.end[1] - self.start[1]) + 1)


def parse_date_range(text: str, today: date | None = None) -> DateRange | None:
    """'Dec 2013 to Current', '05/2014 - 04/2016', 'October 2010 to July 2015', '2013 – 2017', '1999'."""
    today = today or date.today()
    dates = [(m.group(0), m.start()) for m in _DATE_RE.finditer(text)]
    if not dates:
        return None
    start = _parse_date(dates[0][0])
    if not start:
        return None
    if len(dates) >= 2:
        end = _parse_date(dates[1][0], end=True)
        if end:
            if end < start:
                return None
            return DateRange(start, end)
    if _CURRENT_RE.search(text[dates[0][1] + len(dates[0][0]):]):
        return DateRange(start, (today.year, today.month), open_ended=True)
    if len(dates) == 1 and _SEP_RE.search(text[dates[0][1] + len(dates[0][0]):]) is None:
        return DateRange(start, _parse_date(dates[0][0], end=True) or start)
    return None


@dataclass
class Entry:
    title: str | None = None
    meta: str | None = None
    company: str | None = None
    location: str | None = None
    dates_text: str | None = None
    dates: DateRange | None = None
    lines: list[str] = field(default_factory=list)   # bullets and paragraphs, bullet marker stripped

    @property
    def text(self) -> str:
        return "\n".join(l for l in ([self.title] if self.title else []) + self.lines)

    @property
    def bullets(self) -> list[str]:
        return list(self.lines)


@dataclass
class Section:
    title: str
    kind: str
    entries: list[Entry] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(e.text for e in self.entries)

    @property
    def lines(self) -> list[str]:
        out: list[str] = []
        for e in self.entries:
            if e.title:
                out.append(e.title)
            out.extend(e.lines)
        return out


@dataclass
class ParsedDoc:
    headline: str = ""
    sections: list[Section] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Section]:
        return [s for s in self.sections if s.kind == kind]

    @property
    def experience_entries(self) -> list[Entry]:
        return [e for s in self.by_kind("experience") for e in s.entries]


def section_kind(title: str) -> str:
    for kind, pat in KIND_PATTERNS:
        if pat.search(title):
            return kind
    return "other"


def _parse_meta(line: str, entry: Entry, today: date | None) -> None:
    inner = line.strip()[1:-1].strip()
    entry.meta = inner
    parts = [p.strip() for p in inner.split("·")]
    head = parts[0]
    tail = " · ".join(parts[1:]) if len(parts) > 1 else ""
    if " — " in head:
        entry.company, entry.location = (x.strip() or None for x in head.split(" — ", 1))
    elif _DATE_RE.search(head) and not tail:
        tail, head = head, ""
        entry.company = None
    else:
        entry.company = head or None
    if tail:
        entry.dates_text = tail
        entry.dates = parse_date_range(tail, today)


def parse(markdown: str, today: date | None = None) -> ParsedDoc:
    doc = ParsedDoc()
    section: Section | None = None
    entry: Entry | None = None
    for raw in markdown.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# ") and not doc.headline and section is None:
            doc.headline = line[2:].strip()
            continue
        if line.startswith("## "):
            title = line[3:].strip()
            section = Section(title=title, kind=section_kind(title))
            doc.sections.append(section)
            entry = None
            continue
        if section is None:
            section = Section(title="Preamble", kind="summary")
            doc.sections.append(section)
        if line.startswith("### "):
            entry = Entry(title=line[4:].strip())
            section.entries.append(entry)
            continue
        if entry is None:
            entry = Entry()
            section.entries.append(entry)
        if line.startswith("*") and line.endswith("*") and len(line) > 2 and not line.startswith("**"):
            _parse_meta(line, entry, today)
            continue
        text = line.strip()
        if text.startswith("- "):
            text = text[2:].strip()
        entry.lines.append(text)
    return doc


def total_years(entries: list[Entry], today: date | None = None) -> float | None:
    """Union of experience date ranges in years (overlapping jobs are not double-counted)."""
    ranges = [e.dates for e in entries if e.dates]
    ranges = [r for r in ranges if 1950 <= r.start[0] <= (today or date.today()).year and r.months() <= 50 * 12]
    if not ranges:
        return None
    spans = sorted((r.start[0] * 12 + r.start[1], r.end[0] * 12 + r.end[1]) for r in ranges)
    total = 0
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s <= cur_e + 1:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s + 1
            cur_s, cur_e = s, e
    total += cur_e - cur_s + 1
    return round(total / 12, 1)
