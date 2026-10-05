"""Deterministic extraction (free, always runs): years, education, dictionary terms.

Rules:
- years from experience date ranges, merged so overlapping jobs are not counted twice;
- dictionary match per section: skills/summary/other sections give `listed`,
  experience sections give `used` (or `led` when the bullet opens with a
  leadership verb); education, personal and location lines are never matched;
- an ambiguous form counts only with context (see vocab.resolve).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..vocab.resolve import Vocabulary, tokenize
from .sections import LED_RE, ParsedDoc, parse, total_years

LEVEL_RANK = {"listed": 1, "used": 2, "led": 3}

_EDU = [
    ("phd", re.compile(r"\b(ph\.?\s?d\.?|doctor of|doctorate|d\.?phil|dr\.? of)\b", re.I)),
    ("master", re.compile(r"\b(master(?:'s|s)?(?: of| in| degree)?|m\.?s\.?c?\.?|m\.?a\.?|mba|m\.?eng\.?|m\.?ed\.?|ll\.?m\.?|m\.?phil|msc|meng)\b", re.I)),
    ("bachelor", re.compile(r"\b(bachelor(?:'s|s)?(?: of| in| degree)?|b\.?s\.?c?\.?|b\.?a\.?|b\.?e\.?|b\.?tech\.?|b\.?eng\.?|bba|bsn|ll\.?b\.?|bsc|beng|b\.?com)\b", re.I)),
    ("associate", re.compile(r"\b(associate(?:'s|s)? (?:degree|of|in)|a\.?a\.?s?\.?|a\.?s\.?)\b", re.I)),
    ("high_school", re.compile(r"\b(high school|ged|secondary school|hs diploma|diploma)\b", re.I)),
]
_EDU_STRONG = [
    ("phd", re.compile(r"\b(ph\.?d|doctorate)\b", re.I)),
    ("master", re.compile(r"\b(master(?:'s|s)? (?:of|in|degree)|mba)\b", re.I)),
    ("bachelor", re.compile(r"\b(bachelor(?:'s|s)? (?:of|in|degree))\b", re.I)),
]


@dataclass
class DocTerm:
    slug: str
    level: str
    via: str
    evidence: str
    section: str


@dataclass
class DetProfile:
    headline: str
    current_title: str | None
    years: float | None
    years_source: str | None
    education: str | None
    terms: list[DocTerm] = field(default_factory=list)
    ambiguous_rejected: int = 0


def education_level(doc: ParsedDoc, full_text: str) -> str | None:
    edu_text = "\n".join(s.text for s in doc.by_kind("education"))
    if edu_text:
        for level, pat in _EDU:
            if pat.search(edu_text):
                return level
    for level, pat in _EDU_STRONG:
        if pat.search(full_text):
            return level
    return None


def extract(markdown: str, vocab: Vocabulary, today: date | None = None) -> DetProfile:
    doc = parse(markdown, today)
    exp_entries = doc.experience_entries
    years = total_years(exp_entries, today)
    # the most recent job title — only from an entry that looks like a job (company/dates line);
    # the Kaggle layout titles its entries with the company name and has no such line
    current_title = next((e.title for e in exp_entries if e.title and (e.meta or e.dates)), None) or doc.headline or None
    prof = DetProfile(
        headline=doc.headline,
        current_title=current_title,
        years=years,
        years_source="computed" if years is not None else None,
        education=education_level(doc, markdown),
    )

    # pass 1: hits per section (skipping education / personal), plus document-level forms
    per_section: list[tuple[str, str, list[str], list]] = []   # (section title, level, lines, hits per line)
    doc_forms: set[str] = set()
    if doc.headline:
        per_section.append(("headline", "listed", [doc.headline], None))
    for sec in doc.sections:
        if sec.kind in ("education", "skip"):
            continue
        level = "used" if sec.kind == "experience" else "listed"
        per_section.append((sec.title, level, sec.lines, None))

    scanned = []
    for title, level, lines, _ in per_section:
        line_hits = []
        section_tokens: set[str] = set()
        for line in lines:
            toks = tokenize(line)
            section_tokens.update(toks)
            hits = vocab.find(toks)
            line_hits.append((line, hits))
            for h in hits:
                if not h.ambiguous:
                    doc_forms.add(h.form.lower())
        scanned.append((title, level, line_hits, section_tokens))

    # pass 2a: which terms are confirmed anywhere in the document — by a non-ambiguous form, or by an
    # ambiguous form with context in its section. Once confirmed, every mention of that term counts
    # ("Excel" next to Word/PowerPoint in Skills confirms "Reporting in Excel" in Experience).
    confirmed: set[str] = set()
    for title, level, line_hits, section_tokens in scanned:
        for line, hits in line_hits:
            for h in hits:
                if not h.ambiguous or vocab.confirm_ambiguous(h, section_tokens, doc_forms):
                    confirmed.add(h.slug)

    # pass 2b: keep the best level per confirmed term
    best: dict[str, DocTerm] = {}
    for title, level, line_hits, section_tokens in scanned:
        for line, hits in line_hits:
            for h in hits:
                if h.ambiguous and h.slug not in confirmed:
                    prof.ambiguous_rejected += 1
                    continue
                lvl = level
                if level == "used" and LED_RE.match(line):
                    lvl = "led"
                cur = best.get(h.slug)
                if cur is None or LEVEL_RANK[lvl] > LEVEL_RANK[cur.level]:
                    best[h.slug] = DocTerm(h.slug, lvl, "dict", line[:200], title)
    prof.terms = sorted(best.values(), key=lambda t: t.slug)
    return prof
