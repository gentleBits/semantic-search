"""Synthetic hourly rates for documents that carry none (the two real CSVs).

rate = category base × seniority multiplier × noise (±20 %) seeded by the document id, so a rebuild
gives the same numbers; whole euros in [15, 250]. Stored with rate_source = "synthetic" so that facets
and cards can say so.
"""

from __future__ import annotations

import hashlib
import re

CATEGORY_BASE: dict[str, int] = {
    # LiveCareer industries
    "HR": 45, "INFORMATION-TECHNOLOGY": 70, "BUSINESS-DEVELOPMENT": 55, "ADVOCATE": 60,
    "CHEF": 30, "ENGINEERING": 65, "ACCOUNTANT": 50, "FITNESS": 30, "FINANCE": 60,
    "SALES": 45, "AVIATION": 55, "HEALTHCARE": 50, "CONSULTANT": 75, "BANKING": 55,
    "CONSTRUCTION": 45, "PUBLIC-RELATIONS": 50, "DESIGNER": 50, "ARTS": 35, "TEACHER": 35,
    "APPAREL": 40, "DIGITAL-MEDIA": 50, "AGRICULTURE": 35, "AUTOMOBILE": 40, "BPO": 25,
    # Kaggle job titles
    "Java Developer": 65, "Database": 60, "Data Science": 75, "Advocate": 60,
    "Automation Testing": 55, "DevOps Engineer": 70, "Hadoop": 65, "DotNet Developer": 60,
    "Testing": 45, "Arts": 35, "Health and fitness": 30, "Civil Engineer": 50,
    "Business Analyst": 60, "SAP Developer": 70, "Python Developer": 65,
    "Mechanical Engineer": 50, "Sales": 45, "Electrical Engineering": 50,
    "Network Security Engineer": 70, "ETL Developer": 65, "Blockchain": 80,
    "Web Designing": 50, "Operations Manager": 55, "PMO": 60,
}
DEFAULT_BASE = 50

SENIORITY_MULT: dict[str, float] = {
    "intern": 0.5, "junior": 0.7, "mid": 1.0, "senior": 1.3, "lead": 1.5,
    "principal": 1.6, "manager": 1.4, "director": 1.7, "executive": 2.0,
}

_HINTS: list[tuple[str, re.Pattern]] = [
    ("intern", re.compile(r"\b(intern|trainee|apprentice)\b", re.I)),
    ("executive", re.compile(r"\b(chief|ceo|cfo|cto|coo|cio|vp|vice president|president|owner|founder|partner)\b", re.I)),
    ("director", re.compile(r"\bdirector\b", re.I)),
    ("manager", re.compile(r"\b(manager|head of|supervisor)\b", re.I)),
    ("principal", re.compile(r"\b(principal|architect|staff)\b", re.I)),
    ("lead", re.compile(r"\b(lead|team lead|sr\.? lead)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|associate|assistant|entry)\b", re.I)),
]


def seniority_hint(headline: str) -> str:
    for name, pat in _HINTS:
        if pat.search(headline or ""):
            return name
    return "mid"


def synthetic_rate(category: str | None, doc_id: str, seniority: str | None = None, headline: str = "") -> int:
    base = CATEGORY_BASE.get(category or "", DEFAULT_BASE)
    sen = seniority or seniority_hint(headline)
    mult = SENIORITY_MULT.get(sen, 1.0)
    u = int(hashlib.sha256(f"rate:{doc_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    noise = 0.8 + 0.4 * u
    return max(15, min(250, round(base * mult * noise)))
