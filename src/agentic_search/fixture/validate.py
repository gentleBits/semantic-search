"""Check a generated fixture resume against its persona. Returns a list of violations (empty = ok)."""

from __future__ import annotations

import re

HEADINGS = ["## Summary", "## Skills", "## Experience", "## Education"]
MIN_WORDS, MAX_WORDS = 300, 800
MIN_WORDS_JUNIOR = 200  # 1–3 years and two or three short roles: a real junior CV is short
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def min_words(persona: dict) -> int:
    return MIN_WORDS_JUNIOR if persona.get("seniority") == "junior" else MIN_WORDS


def sections(md: str) -> dict[str, str]:
    out: dict[str, str] = {}
    cur = "_head"
    buf: list[str] = []
    for line in md.split("\n"):
        if line.startswith("## "):
            out[cur] = "\n".join(buf)
            cur, buf = line[3:].strip().lower(), []
        else:
            buf.append(line)
    out[cur] = "\n".join(buf)
    return out


def _has(text: str, phrase: str) -> bool:
    # word boundaries that also treat "+" and "#" as word characters, so "C" ≠ "C++" / "C#"
    return re.search(r"(?<![\w+#])" + re.escape(phrase) + r"(?![\w+#])", text, re.I) is not None


def check(md: str, persona: dict) -> list[str]:
    v: list[str] = []
    if md.lstrip().startswith("---"):
        v.append("starts with front matter (must be the resume body only)")
    if not md.lstrip().startswith("# "):
        v.append("must start with a `# <title>` line")
    for h in HEADINGS:
        if f"\n{h}" not in "\n" + md:
            v.append(f"missing heading {h}")
    n = len(md.split())
    lo = min_words(persona)
    if not lo <= n <= MAX_WORDS:
        v.append(f"{n} words (need {lo}–{MAX_WORDS})")

    sec = sections(md)
    exp = sec.get("experience", "")
    skills = sec.get("skills", "")

    for phrase in persona["required"]:
        if not _has(md, phrase):
            v.append(f'required phrase missing: "{phrase}"')
    for pat in persona["forbidden"]:
        m = re.search(pat, md, re.I)
        if m:
            v.append(f'forbidden text present: "{m.group(0)}" (rule {pat})')
    for s in persona["skills_used"]:
        if not _has(exp, s):
            v.append(f'skill "{s}" must appear in an Experience bullet')
    for s in persona["skills_listed"]:
        if not _has(skills, s):
            v.append(f'listed skill "{s}" must appear in the Skills section')
        if _has(exp, s):
            v.append(f'listed-only skill "{s}" must NOT appear in the Experience section')

    entries = re.findall(r"^### .+$", exp, re.M)
    if len(entries) < 2:
        v.append("Experience needs at least 2 `### <Job title>` entries")
    if not re.search(r"^\*.+·.+\*$", exp, re.M):
        v.append("each Experience entry needs a `*Company — City · dates*` line")
    if persona.get("special") == "D":
        if _YEAR.search(exp):
            v.append("D: the Experience section must contain no year or date")
        if re.search(r"\b\d+\+?\s*(years|yrs)\b", md, re.I):
            v.append("D: no 'N years' anywhere")
    elif not _YEAR.search(exp):
        v.append("Experience entries need real dates (Mon YYYY)")
    return v
