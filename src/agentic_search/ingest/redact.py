"""Contact-detail redaction: emails, URLs, phone numbers.

Runs at ingestion, before hashing, so the stored corpus never contains them.
Dates, money and years must survive; the phone rule is written to leave
"1999 - 2002", "12.05.2013" and "$2,000,000" alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL = re.compile(
    r"(?i)(?:https?://|www\.)[^\s)<>\]]+"
    r"|\b(?:linkedin\.com|github\.com|gitlab\.com|bitbucket\.org|twitter\.com|x\.com|"
    r"medium\.com|stackoverflow\.com|behance\.net|dribbble\.com)/[^\s)<>\]]+"
)
# candidate digit runs with common separators; validated by _looks_like_phone
_PHONE_CANDIDATE = re.compile(r"(?<![\w/])\(?\+?\d[\d\s().\-]{7,}\d(?![\w/])")


_CITATION_YEAR = re.compile(r"\(\s*(?:19|20)\d{2}\s*\)")
_NOT_PHONE_BEFORE = re.compile(r"(?i)(?:isbn|issn|doi|patent|us|no\.?|#|id|pi)\s*:?\s*$")


def _looks_like_phone(span: str, before: str = "") -> bool:
    groups = re.findall(r"\d+", span)
    digits = sum(len(g) for g in groups)
    if not 10 <= digits <= 15:
        return False
    # date-like: every group is a year or a day/month → not a phone
    if all(len(g) <= 2 or (len(g) == 4 and 1900 <= int(g) <= 2099) for g in groups):
        return False
    # "Materials Letters, 59 (2005) 570-574" — a citation, not a phone
    if _CITATION_YEAR.search(span):
        return False
    # "ISBN: 959-7160-31-5", "US 20050276743 A1", "PI 2013 700 338"
    if _NOT_PHONE_BEFORE.search(before[-8:]):
        return False
    return True


@dataclass
class RedactionCounts:
    emails: int = 0
    urls: int = 0
    phones: int = 0

    def add(self, other: "RedactionCounts") -> None:
        self.emails += other.emails
        self.urls += other.urls
        self.phones += other.phones

    def total(self) -> int:
        return self.emails + self.urls + self.phones


def redact(text: str) -> tuple[str, RedactionCounts]:
    counts = RedactionCounts()

    def _email(m: re.Match) -> str:
        counts.emails += 1
        return "[email]"

    def _url(m: re.Match) -> str:
        counts.urls += 1
        return "[url]"

    def _phone(m: re.Match) -> str:
        if _looks_like_phone(m.group(0), m.string[: m.start()]):
            counts.phones += 1
            return "[phone]"
        return m.group(0)

    text = EMAIL.sub(_email, text)
    text = URL.sub(_url, text)
    text = _PHONE_CANDIDATE.sub(_phone, text)
    return text, counts
