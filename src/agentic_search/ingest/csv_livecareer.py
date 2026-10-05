"""LiveCareer CSV (ID, Resume_str, Resume_html, Category) → markdown.

The HTML carries semantic CSS classes. Mapping:
  div.name                       → `# headline` (first line) + remaining lines as a paragraph
  div.section / div.sectiontitle → `## Section`
  div.paragraph (one entry)      → `### jobtitle` | `### degree, programline`
                                   `*Company Name — City, State · Dec 2013 to Current*`
                                   bullets (`ul/li`, table cells) and paragraphs (`p`)
Resume_str is the fallback when the HTML yields nothing.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from .normalize import collapse
from .types import RawDoc

csv.field_size_limit(10**9)

_HEADER_SELECTOR = (
    ".jobtitle, .degree, .programline, .datesWrapper, .dates_wrapper, .jobdates, "
    ".companyname, .joblocation, .statesWrapper"
)
_INLINE = {"span", "b", "i", "u", "strong", "em", "font", "sub", "sup", "a", "strike", "small"}
_SKIP = {"img", "svg", "script", "style", "circle", "rect", "text"}
_BLOCK_CHILD = {"ul", "ol", "p", "div", "table", "br", "li"}
_PUNCT_ONLY = re.compile(r"^[\s\W_]*$")


def _text(el: Tag | None) -> str:
    return collapse(el.get_text(" ", strip=True)) if el is not None else ""


class _Renderer:
    """Turns an HTML fragment into markdown lines (paragraphs and bullets)."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.buf: list[str] = []

    def flush(self) -> None:
        text = collapse(" ".join(self.buf))
        self.buf = []
        if text and not _PUNCT_ONLY.match(text):
            self._blank()
            self.lines.append(text)

    def _blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def bullet(self, text: str) -> None:
        text = collapse(text)
        if text and not _PUNCT_ONLY.match(text):
            if self.lines and self.lines[-1] and not self.lines[-1].startswith("- "):
                self.lines.append("")
            self.lines.append(f"- {text}")

    def walk(self, el: Tag, in_cell: bool = False) -> None:
        for child in el.children:
            if isinstance(child, NavigableString):
                if child.strip():
                    self.buf.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name in _SKIP:
                continue
            if name in ("ul", "ol"):
                self.flush()
                for li in child.find_all("li", recursive=False):
                    self.bullet(li.get_text(" ", strip=True))
                continue
            if name == "li":
                self.flush()
                self.bullet(child.get_text(" ", strip=True))
                continue
            if name == "br":
                self.flush()
                continue
            if name in _INLINE and not child.find(_BLOCK_CHILD):
                if child.get_text(strip=True):
                    self.buf.append(child.get_text(" ", strip=True))
                continue
            if name == "p" and in_cell:
                # two-column "Highlights" tables: one <p> per highlight
                self.flush()
                self.bullet(child.get_text(" ", strip=True))
                continue
            # block-ish: p, div, table, tr, td, th, tbody, or an inline tag with block children
            self.flush()
            self.walk(child, in_cell=in_cell or name in ("td", "th"))
            self.flush()

    def result(self) -> list[str]:
        self.flush()
        while self.lines and self.lines[-1] == "":
            self.lines.pop()
        while self.lines and self.lines[0] == "":
            self.lines.pop(0)
        return self.lines


def _entry_lines(par: Tag) -> list[str]:
    """One div.paragraph → heading line(s) + body lines."""
    jobtitle = _text(par.find(class_="jobtitle"))
    degree = _text(par.find(class_="degree"))
    program = _text(par.find(class_="programline"))
    dates_el = par.find(class_=re.compile(r"^(datesWrapper|dates_wrapper)"))
    dates = _text(dates_el) if dates_el else " to ".join(
        t for t in (_text(d) for d in par.find_all(class_="jobdates")) if t
    )
    company = _text(par.find(class_=re.compile(r"^companyname")))
    location_parts: list[str] = []
    for loc in par.find_all(class_=re.compile(r"^joblocation")):
        t = _text(loc)
        if t and t not in location_parts:
            location_parts.append(t)
    location = ", ".join(location_parts)

    # A `paddedline` that holds header fields is entirely header material (it also
    # carries the " to ", "－" and "," separators between them): drop the whole line.
    for line in par.find_all(class_="paddedline"):
        if line.select_one(_HEADER_SELECTOR):
            line.decompose()
    for el in par.select(_HEADER_SELECTOR):
        el.decompose()

    out: list[str] = []
    title_parts = [p for p in (degree, program) if p and p.upper() not in ("N/A", "NA", "-")]
    if jobtitle:
        out.append(f"### {jobtitle}")
    elif title_parts:
        out.append(f"### {', '.join(title_parts)}")
    meta = " · ".join(
        p for p in (" — ".join(x for x in (company, location) if x), dates) if p
    )
    if meta:
        out.append(f"*{meta}*")

    r = _Renderer()
    r.walk(par)
    body = r.result()
    if body:
        if out:
            out.append("")
        out.extend(body)
    return out


def _name_lines(name_div: Tag) -> tuple[str, list[str]]:
    raw = name_div.get_text("\n")
    lines: list[str] = []
    for line in raw.split("\n"):
        t = collapse(line)
        if t and (not lines or t != lines[-1]):
            lines.append(t)
    if not lines:
        return "", []
    return lines[0], lines[1:]


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find(id="document") or soup
    sections = [s for s in root.find_all("div", class_="section") if s.find_parent("div", class_="section") is None]
    if not sections:
        return ""

    headline = ""
    head_extra: list[str] = []
    body: list[str] = []
    for sec in sections:
        name_div = sec.find("div", class_="name")
        if name_div is not None:
            h, extra = _name_lines(name_div)
            if h and not headline:
                headline, head_extra = h, extra
                continue
        title_el = sec.find(class_="sectiontitle")
        title = _text(title_el) or "Section"
        if title_el is not None:
            heading = title_el.find_parent("div", class_="heading")
            (heading or title_el).decompose()
        pars = sec.find_all("div", class_="paragraph")
        entries: list[list[str]] = []
        if pars:
            for par in pars:
                lines = _entry_lines(par)
                if lines:
                    entries.append(lines)
        else:
            r = _Renderer()
            r.walk(sec)
            lines = r.result()
            if lines:
                entries.append(lines)
        if not entries:
            continue
        body.append(f"## {title}")
        body.append("")
        for i, lines in enumerate(entries):
            if i:
                body.append("")
            body.extend(lines)
        body.append("")

    out: list[str] = []
    if headline:
        out.append(f"# {headline}")
        out.append("")
        if head_extra:
            out.append(" ".join(head_extra))
            out.append("")
    out.extend(body)
    return "\n".join(out).strip() + "\n"


def str_to_markdown(text: str) -> str:
    """Fallback for rows whose HTML yields nothing: first line as headline, rest as prose."""
    lines = [collapse(l) for l in text.split("\n")]
    lines = [l for l in lines if l]
    if not lines:
        return ""
    headline, rest = lines[0], lines[1:]
    return f"# {headline}\n\n" + "\n\n".join(rest) + "\n"


_FIRST_H3 = re.compile(r"^### (.+)$", re.M)


def _fallback_headline(md: str, category: str | None) -> str:
    """A resume with no name block: use its most recent job title, else the category."""
    m = _FIRST_H3.search(md)
    if m:
        return collapse(m.group(1))
    return (category or "Resume").replace("-", " ").title()


def load(path: Path, source_id: str = "livecareer") -> Iterator[RawDoc]:
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            category = (row.get("Category") or "").strip() or None
            md = html_to_markdown(row.get("Resume_html") or "")
            used = "html"
            if len(md.split()) < 30:
                md = str_to_markdown(row.get("Resume_str") or "")
                used = "str"
            elif not md.startswith("# "):
                md = f"# {_fallback_headline(md, category)}\n\n{md}"
                used = "html+fallback_title"
            yield RawDoc(
                source=source_id,
                source_id=str(row["ID"]).strip(),
                category=(row.get("Category") or "").strip() or None,
                markdown=md,
                meta={"converter": used},
            )
