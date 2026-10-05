"""Vocabulary: load schema/vocab.csv, resolve surface forms, implication closure, dictionary matching.

Matching is token based: text and surface forms are tokenised the same way
(`+`, `#` and inner `.` stay inside tokens, so "C" ≠ "C++" ≠ "C#" and
"node.js" is one token), lower-cased, and looked up as 1–5-token tuples.
An ambiguous form (see the CSV) only counts when a context word occurs in the
same section, or a non-ambiguous form of the same term occurs anywhere in
the document.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN = re.compile(r"[A-Za-z0-9+#]+(?:[.\-/'&][A-Za-z0-9+#]+)*|\.[A-Za-z][A-Za-z0-9+#]*")
MAX_NGRAM = 5


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


@dataclass
class Term:
    term_id: int
    kind: str          # skill | topic
    slug: str
    canonical: str
    aliases: list[str]
    implies: list[str]
    ambiguous: set[str]     # lower-cased surface forms that need context
    context: set[str]       # lower-cased context words
    description: str

    @property
    def forms(self) -> list[str]:
        return [self.canonical] + self.aliases

    @property
    def unambiguous_forms(self) -> list[str]:
        return [f for f in self.forms if f.lower() not in self.ambiguous]


@dataclass
class Hit:
    slug: str
    form: str            # the surface form as written in the vocabulary
    start: int           # token offset
    end: int
    ambiguous: bool


class Vocabulary:
    def __init__(self, terms: list[Term]) -> None:
        self.terms = terms
        self.by_slug = {t.slug: t for t in terms}
        self.by_form: dict[str, Term] = {}
        self._ngram_index: dict[tuple[str, ...], tuple[Term, str]] = {}
        for t in terms:
            for f in t.forms:
                key = f.lower()
                if key in self.by_form and self.by_form[key].slug != t.slug:
                    raise ValueError(f"surface form {f!r} belongs to {self.by_form[key].slug} and {t.slug}")
                self.by_form[key] = t
                toks = tuple(tokenize(f))
                if toks:
                    self._ngram_index[toks] = (t, f)
        self._closure: dict[str, list[str]] = {}
        for t in terms:
            self._closure[t.slug] = self._compute_closure(t.slug)

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path) -> "Vocabulary":
        terms: list[Term] = []
        with path.open(newline="", encoding="utf-8") as f:
            for i, row in enumerate(csv.DictReader(f), start=1):
                aliases = [a for a in row["aliases"].split("|") if a]
                desc = row["description"] or f"{row['canonical']} ({', '.join(aliases)})" if aliases else row["description"] or row["canonical"]
                terms.append(Term(
                    term_id=i,
                    kind=row["kind"],
                    slug=row["slug"],
                    canonical=row["canonical"],
                    aliases=aliases,
                    implies=[s for s in row["implies"].split("|") if s],
                    ambiguous={a.lower() for a in row["ambiguous"].split("|") if a},
                    context={c.lower() for c in row["context"].split("|") if c},
                    description=desc,
                ))
        v = cls(terms)
        for t in terms:
            for s in t.implies:
                if s not in v.by_slug:
                    raise ValueError(f"{t.slug} implies unknown term {s}")
        return v

    # ------------------------------------------------------------ resolution
    def resolve(self, text: str) -> Term | None:
        """Exact / alias match of a whole phrase (case-insensitive, punctuation-tolerant)."""
        key = text.strip().lower()
        if key in self.by_form:
            return self.by_form[key]
        toks = tuple(tokenize(text))
        hit = self._ngram_index.get(toks)
        if hit:
            return hit[0]
        # "data-pipelines" / "data_pipelines" → slug
        slug = re.sub(r"[\s_]+", "-", key)
        return self.by_slug.get(slug)

    def _compute_closure(self, slug: str) -> list[str]:
        seen: list[str] = []
        stack = list(self.by_slug[slug].implies)
        while stack:
            s = stack.pop()
            if s in seen or s == slug:
                continue
            seen.append(s)
            stack.extend(self.by_slug[s].implies)
        return seen

    def implied(self, slug: str) -> list[str]:
        """All terms a document tagged with `slug` is also tagged with (transitive)."""
        return self._closure[slug]

    def implying(self, slug: str) -> list[str]:
        """All terms whose closure contains `slug` (the reverse relation)."""
        return [t.slug for t in self.terms if slug in self._closure[t.slug]]

    # ------------------------------------------------------------ dictionary matching
    def find(self, tokens: list[str]) -> list[Hit]:
        """Longest-match n-gram scan over a token list."""
        hits: list[Hit] = []
        i, n = 0, len(tokens)
        while i < n:
            best = None
            for k in range(min(MAX_NGRAM, n - i), 0, -1):
                key = tuple(tokens[i : i + k])
                got = self._ngram_index.get(key)
                if got:
                    best = (k, got)
                    break
            if best:
                k, (term, form) = best
                hits.append(Hit(term.slug, form, i, i + k, form.lower() in term.ambiguous))
                i += k
            else:
                i += 1
        return hits

    def confirm_ambiguous(self, hit: Hit, section_tokens: set[str], doc_forms: set[str]) -> bool:
        """An ambiguous hit counts with a context word in its section, or a non-ambiguous form of the term in the document."""
        term = self.by_slug[hit.slug]
        if any(f.lower() in doc_forms for f in term.unambiguous_forms):
            return True
        return bool(term.context & section_tokens) or any(
            all(w in section_tokens for w in c.split()) for c in term.context if " " in c
        )
