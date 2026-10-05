"""Chunking: one chunk per section, one per experience entry,
split at line boundaries when over the token budget, each with a context header.

    header:  "Senior Data Engineer | Experience › Acme, Jan 2019 to Dec 2024"
    text:    the entry's title line + bullets
The embedded string is `header + "\\n" + text`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..extract.sections import Entry, ParsedDoc, Section, parse


@dataclass
class Chunk:
    doc_no: int
    idx: int
    section: str        # section kind: summary|skills|experience|education|other|skip
    header: str
    text: str
    tokens: int

    @property
    def embed_text(self) -> str:
        return f"{self.header}\n{self.text}"


def _entry_lines(entry: Entry) -> list[str]:
    lines: list[str] = []
    if entry.title:
        lines.append(entry.title)
    lines.extend(entry.lines)
    return lines


def _header(headline: str, section: Section, entry: Entry | None) -> str:
    h = headline or "Resume"
    if entry is None or (not entry.title and not entry.company):
        return f"{h} | {section.title}"
    parts = [p for p in (entry.company, entry.dates_text) if p]
    tail = f"{entry.title or ''}" + (f", {', '.join(parts)}" if parts else "")
    return f"{h} | {section.title} › {tail.strip(', ')}"


def _hard_split(line: str, count: Callable[[str], int], budget: int) -> list[str]:
    """Binary word split until every piece fits the budget (a single word never splits)."""
    words = line.split()
    if count(line) <= budget or len(words) <= 1:
        return [line]
    mid = len(words) // 2
    return _hard_split(" ".join(words[:mid]), count, budget) + _hard_split(" ".join(words[mid:]), count, budget)


def _split(lines: list[str], count: Callable[[str], int], max_tokens: int, overlap: int) -> list[list[str]]:
    """Greedy line packing; a group starts with the tail of the previous one (≈ overlap tokens)."""
    groups: list[list[str]] = []
    cur: list[str] = []
    cur_tokens = 0
    for line in lines:
        n = count(line) + 1  # +1: the newline that joins it to the next line is a token too
        if n > max_tokens:  # one giant line (a comma-separated skills paragraph, say)
            if cur:
                groups.append(cur)
                cur, cur_tokens = [], 0
            groups.extend([piece] for piece in _hard_split(line, count, max_tokens - 1))
            continue
        if cur and cur_tokens + n > max_tokens:
            groups.append(cur)
            tail: list[str] = []
            t = 0
            for prev in reversed(cur):
                pn = count(prev) + 1
                if t + pn > overlap:
                    break
                tail.insert(0, prev)
                t += pn
            if t + n > max_tokens:  # the overlap would push the new group over budget: start clean
                tail, t = [], 0
            cur, cur_tokens = tail, t
        cur.append(line)
        cur_tokens += n
    if cur:
        groups.append(cur)
    return groups


def chunk_document(
    doc_no: int,
    markdown: str,
    count: Callable[[str], int],
    max_tokens: int = 350,
    overlap: int = 40,
    parsed: ParsedDoc | None = None,
) -> list[Chunk]:
    doc = parsed or parse(markdown)
    chunks: list[Chunk] = []

    def emit(section: Section, entry: Entry | None, lines: list[str]) -> None:
        if not lines:
            return
        header = _header(doc.headline, section, entry)
        header_tokens = count(header)
        budget = max(50, max_tokens - header_tokens - 1)
        for group in _split(lines, count, budget, overlap):
            text = "\n".join(group)
            chunks.append(Chunk(doc_no, len(chunks), section.kind, header, text, header_tokens + 1 + count(text)))

    for section in doc.sections:
        if section.kind == "skip":
            continue
        if section.kind == "experience":
            for entry in section.entries:
                emit(section, entry, _entry_lines(entry))
        else:
            lines: list[str] = []
            for entry in section.entries:
                lines.extend(_entry_lines(entry))
            emit(section, None, lines)
    if not chunks and markdown.strip():
        text = markdown.strip()
        chunks.append(Chunk(doc_no, 0, "other", doc.headline or "Resume", text, count(text)))
    return chunks
