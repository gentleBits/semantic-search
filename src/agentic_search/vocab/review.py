"""`resumes vocab review|add|alias|merge`: edit schema/vocab.csv; the next `index build` applies it."""

from __future__ import annotations

import csv
import difflib
import io
import json
import re
from pathlib import Path

from ..errors import ResumesError

FIELDS = ["kind", "slug", "canonical", "aliases", "implies", "ambiguous", "context", "description"]
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_rows(path: Path, rows: list[dict]) -> None:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows({k: r.get(k, "") for k in FIELDS} for r in rows)
    tmp = path.with_suffix(".csv.tmp")
    tmp.write_text(out.getvalue(), encoding="utf-8")
    tmp.replace(path)


def forms_of(row: dict) -> list[str]:
    return [row["canonical"]] + [a for a in row["aliases"].split("|") if a]


def lint(rows: list[dict]) -> None:
    owner: dict[str, str] = {}
    for r in rows:
        if not SLUG.fullmatch(r["slug"]):
            raise ResumesError("VOCAB_INVALID", f"bad slug {r['slug']!r}")
        for f in forms_of(r):
            k = f.lower()
            if k in owner and owner[k] != r["slug"]:
                raise ResumesError("VOCAB_INVALID", f"form {f!r} belongs to both {owner[k]} and {r['slug']}")
            owner[k] = r["slug"]
    slugs = {r["slug"] for r in rows}
    for r in rows:
        for s in (x for x in r["implies"].split("|") if x):
            if s not in slugs:
                raise ResumesError("VOCAB_INVALID", f"{r['slug']} implies unknown {s}")


def review(vocab_path: Path, unresolved_path: Path, top: int = 30) -> list[dict]:
    """The most frequent unresolved names from the LLM extraction, with the nearest vocabulary form."""
    if not unresolved_path.is_file():
        raise ResumesError("NO_UNRESOLVED_TERMS", f"{unresolved_path} (written by the LLM extraction step)")
    rows = load_rows(vocab_path)
    forms = {f.lower(): r["slug"] for r in rows for f in forms_of(r)}
    items = json.loads(unresolved_path.read_text(encoding="utf-8"))
    out = []
    for name, count in items[:top]:
        near = difflib.get_close_matches(name.lower(), list(forms), n=1, cutoff=0.75)
        out.append({"name": name, "count": count, "already": forms.get(name.lower()), "nearest": (near[0], forms[near[0]]) if near else None})
    return out


def add(vocab_path: Path, *, kind: str, slug: str, canonical: str, aliases: list[str] | None = None, implies: list[str] | None = None,
        description: str = "") -> dict:
    if kind not in ("skill", "topic"):
        raise ResumesError("VOCAB_INVALID", "kind is skill or topic")
    rows = load_rows(vocab_path)
    if any(r["slug"] == slug for r in rows):
        raise ResumesError("VOCAB_EXISTS", slug)
    row = {"kind": kind, "slug": slug, "canonical": canonical, "aliases": "|".join(aliases or []), "implies": "|".join(implies or []),
           "ambiguous": "", "context": "", "description": description}
    rows.append(row)
    lint(rows)
    save_rows(vocab_path, rows)
    return row


def alias(vocab_path: Path, slug: str, form: str) -> dict:
    rows = load_rows(vocab_path)
    row = next((r for r in rows if r["slug"] == slug), None)
    if row is None:
        raise ResumesError("UNRESOLVED_TERM", slug)
    if form.lower() in {f.lower() for f in forms_of(row)}:
        return row
    row["aliases"] = "|".join([a for a in row["aliases"].split("|") if a] + [form])
    lint(rows)
    save_rows(vocab_path, rows)
    return row


def merge(vocab_path: Path, src: str, dst: str) -> dict:
    """Fold `src` into `dst`: its forms become aliases of dst, references to src point at dst, src is removed."""
    rows = load_rows(vocab_path)
    s = next((r for r in rows if r["slug"] == src), None)
    d = next((r for r in rows if r["slug"] == dst), None)
    if s is None or d is None or src == dst:
        raise ResumesError("UNRESOLVED_TERM", f"{src} → {dst}")
    have = {f.lower() for f in forms_of(d)}
    d["aliases"] = "|".join([a for a in d["aliases"].split("|") if a] + [f for f in forms_of(s) if f.lower() not in have])
    d["implies"] = "|".join(sorted({x for x in (d["implies"] + "|" + s["implies"]).split("|") if x and x != dst}))
    rows = [r for r in rows if r["slug"] != src]
    for r in rows:
        r["implies"] = "|".join(sorted({dst if x == src else x for x in r["implies"].split("|") if x and not (x == src and r["slug"] == dst)}))
    lint(rows)
    save_rows(vocab_path, rows)
    return d
