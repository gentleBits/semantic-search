"""Canonical text normalisation applied to every document before hashing.

Leading indentation is preserved (nested markdown lists).
"""

from __future__ import annotations

import re
import unicodedata

import ftfy

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_INNER_SPACES = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")
_MANY_BLANKS = re.compile(r"\n{3,}")


def normalize(text: str) -> str:
    # mojibake repair first ("â€”" → "—", "NaÃ¯ve" → "Naïve"); ftfy leaves clean text alone
    text = ftfy.fix_text(text)
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace("\t", " ")
    lines = []
    for line in text.split("\n"):
        line = _INNER_SPACES.sub(" ", line.rstrip())
        # a line that is only punctuation left over from a stripped layout is noise
        if line.strip() and not re.search(r"[\w]", line):
            continue
        lines.append(line)
    out = "\n".join(lines)
    out = _MANY_BLANKS.sub("\n\n", out).strip()
    return out + "\n" if out else ""


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()
