"""Kaggle "UpdatedResumeDataSet" CSV (Category, Resume) → markdown.

The text is semi-structured:
  [skills prefix]  Education Details ... <Title> / <Title> - <Company>
  Skill Details  <skill>- Exprience - N months ...
  Company Details  company - X / description - ...
Markers are sometimes glued to the previous token ("24 monthsCompany Details").
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from pathlib import Path

import ftfy

from .normalize import collapse
from .types import RawDoc

csv.field_size_limit(10**9)

MARKERS = ("Education Details", "Skill Details", "Company Details")
_SKILL_LINE = re.compile(r"^(?P<skill>.+?)\s*-\s*Exprience\s*-\s*(?P<exp>.+?)\s*$", re.I)
_BULLET_START = re.compile(r"^\s*(?:[*•●▪■\-–]|\d+[.)])\s+")


def _own_line(text: str) -> str:
    for m in MARKERS:
        text = re.sub(r"[ \t]*" + re.escape(m) + r"[ \t]*\n?", "\n" + m + "\n", text)
    return text


def _segments(text: str) -> dict[str, list[str]]:
    segs: dict[str, list[str]] = {"pre": [], **{m: [] for m in MARKERS}}
    cur = "pre"
    for line in text.split("\n"):
        s = line.strip()
        if s in MARKERS:
            cur = s
            continue
        segs[cur].append(line.rstrip())
    return segs


def _headline(edu_lines: list[str]) -> tuple[str, str, list[str]]:
    """Find `<Title>` followed by `<Title> - <Company>`; return (title, current, education lines)."""
    idx = [i for i, l in enumerate(edu_lines) if l.strip()]
    for a, i in enumerate(idx[:-1]):
        j = idx[a + 1]
        t, n = edu_lines[i].strip(), edu_lines[j].strip()
        if n == t or n.startswith(t + " - ") or n.startswith(t + " -"):
            rest = [edu_lines[k] for k in idx[a + 2 :]]
            return t, n if n != t else "", [edu_lines[k] for k in idx[:a]] + rest
    return "", "", [edu_lines[k] for k in idx]


def _bullets_from_prefix(lines: list[str]) -> list[str]:
    text = collapse(" ".join(lines))
    if not text:
        return []
    text = re.sub(r"^(Skills?|Technical Skills|TECHNICAL SKILLS|Key Skills|KEY SKILLS)\s*[:*•\-]*\s*", "", text, flags=re.I)
    parts = [collapse(p) for p in re.split(r"\s[*•●▪]\s|\s\*\s*|\s•\s*", text)]
    parts = [p for p in parts if p]
    if len(parts) > 1:
        return [f"- {p}" for p in parts]
    return [text]


def _company_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(collapse(" ".join(para)))
            out.append("")
            para.clear()

    for line in lines:
        s = line.strip()
        if not s:
            flush()
            continue
        m = re.match(r"^company\s*-\s*(.*)$", s, re.I)
        if m:
            flush()
            if out and out[-1] != "":
                out.append("")
            out.append(f"### {collapse(m.group(1)) or 'Company'}")
            out.append("")
            continue
        m = re.match(r"^description\s*-\s*(.*)$", s, re.I)
        if m:
            flush()
            s = m.group(1).strip()
            if not s:
                continue
        if _BULLET_START.match(s):
            flush()
            out.append(f"- {collapse(_BULLET_START.sub('', s))}")
            continue
        # inline " * " / " • " separators inside a line: text before is prose, the rest are bullets
        parts = [p for p in re.split(r"\s[*•●▪]\s+", s) if p.strip()]
        if len(parts) > 1:
            if parts[0].strip():
                para.append(parts[0].strip())
            flush()
            out.extend(f"- {collapse(p)}" for p in parts[1:])
            continue
        if out and out[-1].startswith("- ") and not para:
            out.append("")
        para.append(s)
    flush()
    while out and out[-1] == "":
        out.pop()
    return out


def text_to_markdown(raw: str, category: str | None) -> str:
    text = ftfy.fix_text(raw).replace("\r\n", "\n").replace("\r", "\n")
    segs = _segments(_own_line(text))

    title, current, edu = _headline(segs["Education Details"])
    headline = title or category or "Resume"

    skills: list[str] = _bullets_from_prefix(segs["pre"])
    for line in segs["Skill Details"]:
        s = line.strip()
        if not s:
            continue
        m = _SKILL_LINE.match(s)
        skills.append(f"- {collapse(m['skill'])} — {collapse(m['exp'])}" if m else f"- {collapse(s)}")

    out = [f"# {headline}", ""]
    if current:
        out += [f"*{collapse(current)}*", ""]
    if skills:
        out += ["## Skills", ""]
        out += skills
        out.append("")
    exp = _company_lines(segs["Company Details"])
    if exp:
        out += ["## Experience", ""] + exp + [""]
    if edu:
        out += ["## Education", ""] + [f"- {collapse(l)}" for l in edu if l.strip()] + [""]
    return "\n".join(out).strip() + "\n"


def load(path: Path, source_id: str = "kaggle") -> Iterator[RawDoc]:
    import hashlib

    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = row.get("Resume") or ""
            sid = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
            if sid in seen:  # the file repeats each text ~6×; identical rows are one source row
                continue
            seen.add(sid)
            yield RawDoc(
                source=source_id,
                source_id=sid,
                category=(row.get("Category") or "").strip() or None,
                markdown=text_to_markdown(raw, (row.get("Category") or "").strip() or None),
                meta={"converter": "kaggle_text"},
            )
