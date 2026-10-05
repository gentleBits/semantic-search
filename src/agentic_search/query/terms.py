"""Resolving the user's words to vocabulary terms.

    "data pipelines"  → topic data-pipelines
    "ETL stuff"       → skill etl → topic data-pipelines (a skill asked for as a topic maps to the topic it implies)
    "elixer"          → UNRESOLVED_TERM … did you mean elixir (0.83)?
    "moved data nightly between systems" → nothing: the caller falls back to text search
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ResumesError
from ..vocab.resolve import Term, Vocabulary, tokenize

SUGGEST_CUTOFF = 0.8


@dataclass
class Resolved:
    given: str          # what the user typed
    slug: str
    kind: str           # topic | skill
    canonical: str
    how: str            # exact | phrase | via-skill:<slug>

    @property
    def echo(self) -> str:
        """`"ETL stuff" → data-pipelines (via etl)`."""
        via = f" (via {self.how.split(':', 1)[1]})" if self.how.startswith("via-skill:") else ""
        return f'"{self.given}" → {self.slug}{via}'


def _with_hint(vocab: Vocabulary, given: str, t: Term, how: str, hint: str) -> Resolved:
    if hint == "topic" and t.kind == "skill":
        topics = [s for s in vocab.implied(t.slug) if vocab.by_slug[s].kind == "topic"]
        if len(topics) == 1:
            tt = vocab.by_slug[topics[0]]
            return Resolved(given, tt.slug, tt.kind, tt.canonical, f"via-skill:{t.slug}")
    return Resolved(given, t.slug, t.kind, t.canonical, how)


def resolve_one(vocab: Vocabulary, phrase: str, hint: str) -> list[Resolved]:
    """Terms found in one phrase, in order of appearance; [] when nothing in it is a term."""
    t = vocab.resolve(phrase)
    if t is not None:
        return [_with_hint(vocab, phrase, t, "exact", hint)]
    out: list[Resolved] = []
    seen: set[str] = set()
    for h in vocab.find(tokenize(phrase)):
        if h.slug in seen:
            continue
        seen.add(h.slug)
        out.append(_with_hint(vocab, phrase, vocab.by_slug[h.slug], "phrase", hint))
    return out


def suggestion(vocab: Vocabulary, phrase: str) -> tuple[str, float] | None:
    """The closest vocabulary form to an unresolved phrase, if close enough to be a typo."""
    import difflib

    key = phrase.strip().lower()
    match = difflib.get_close_matches(key, list(vocab.by_form), n=1, cutoff=SUGGEST_CUTOFF)
    if not match:
        return None
    return vocab.by_form[match[0]].slug, difflib.SequenceMatcher(None, key, match[0]).ratio()


def resolve_phrases(vocab: Vocabulary, phrases: list[str], hint: str) -> tuple[list[Resolved], list[str]]:
    """(resolved terms, phrases nothing matched). A near-miss raises UNRESOLVED_TERM with the suggestion."""
    resolved: list[Resolved] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        phrase = phrase.strip()
        if not phrase:
            continue
        found = resolve_one(vocab, phrase, hint)
        if not found:
            s = suggestion(vocab, phrase)
            if s:
                raise ResumesError("UNRESOLVED_TERM", f'"{phrase}" → did you mean {s[0]} ({s[1]:.2f})?')
            unresolved.append(phrase)
            continue
        for r in found:
            if r.slug not in seen:
                seen.add(r.slug)
                resolved.append(r)
    return resolved, unresolved


def expansion(vocab: Vocabulary, slug: str) -> list[str]:
    """The skills that imply `slug`, already folded into a topic's membership."""
    return vocab.implying(slug)
