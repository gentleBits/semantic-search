"""Document-to-document matching: a job description (or a resume) → ranked resumes.

    requirements = the JD's bullets / lines (≥ 3 words, ≤ 40 of them)
    cover(d)     = mean over requirement lines of the best weight among its terms:
                   1.0 used in a job · 0.8 strong evidence · 0.5 semantic only
    members      = docs covering ≥ like_min_cover of the resolved lines; only when no line resolves:
                   docs whose best chunk is ≥ tau_text from an unresolved line
    order        = RRF over cover, cosine(whole JD, best chunk), BM25 of the covered terms
With explicit --topic/--skill/--text those decide membership and --like only ranks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pyroaring import BitMap

from ..config import Config
from ..errors import ResumesError
from ..vocab.resolve import tokenize
from .index import Index
from .text import QueryEmbedder

BULLET = re.compile(r"^\s*(?:[-*•▪◦]|\d+[.)])\s+")
MAX_REQUIREMENTS = 40
MAX_UNRESOLVED_EMBED = 5
WEIGHT = {"used": 1.0, "strong": 0.8, "weak": 0.5}


@dataclass
class Requirement:
    text: str
    slugs: list[str] = field(default_factory=list)


@dataclass
class LikeResult:
    source: str
    requirements: list[Requirement]
    resolved: list[str]
    unresolved: list[str]
    min_cover: float
    members: BitMap
    strong: BitMap
    cover: dict[int, float]
    cosine: dict[int, float]
    bm25: dict[int, float]

    @property
    def summary(self) -> str:
        shown = ", ".join(self.resolved[:6]) + (f", +{len(self.resolved) - 6}" if len(self.resolved) > 6 else "")
        s = f"like {self.source} (requirements {len(self.requirements)}, resolved {len(self.resolved)}: {shown or '—'}"
        if self.unresolved:
            s += f"; unresolved {len(self.unresolved)}"
        return s + ")"


def read_source(index: Index, source: str, session_dir: Path | None) -> tuple[str, str]:
    """(label, text) for a file path (bare names resolve against the session dir first) or a document id."""
    p = Path(source)
    candidates = [p] if p.is_absolute() else ([session_dir / p] if session_dir else []) + [p]
    for c in candidates:
        if c.is_file():
            return c.name, c.read_text(encoding="utf-8")
    ref = source.strip()
    if re.fullmatch(r"r\d{6}|\d+", ref):
        prof = index.doc(ref)
        md = index.markdown(prof.doc_no) or ""
        return prof.id, md
    raise ResumesError("LIKE_SOURCE_NOT_FOUND", f"{source}: not a file (looked in the session dir and here) and not a document id")


def parse_requirements(text: str) -> list[str]:
    reqs: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        line = BULLET.sub("", line).strip(" .;")
        if not line or line.endswith(":"):                  # "Nice to have:" headers
            continue
        reqs.append(line)
        if len(reqs) >= MAX_REQUIREMENTS:
            break
    return reqs


def analyse(index: Index, cfg: Config, source: str, text: str, *, min_cover: float | None = None) -> LikeResult:
    vocab = index.vocab_with_context
    min_cover = cfg.query.like_min_cover if min_cover is None else min_cover
    reqs = [Requirement(r) for r in parse_requirements(text)]
    resolved: list[str] = []
    jd_tokens = set(tokenize(text))
    for r in reqs:
        line_tokens = tokenize(r.text)
        for h in vocab.find(line_tokens):
            # the whole (short) JD is the context window: "Spring" counts when the JD says Java
            if h.ambiguous and not vocab.confirm_ambiguous(h, jd_tokens, jd_tokens):
                continue
            if h.slug not in r.slugs:
                r.slugs.append(h.slug)
            if h.slug not in resolved:
                resolved.append(h.slug)
    reqs = [r for r in reqs if r.slugs or len(r.text.split()) >= 3]     # a bare term is a requirement; a stray short line is not
    if not reqs:
        raise ResumesError("LIKE_EMPTY", f"{source}: no requirement lines found")
    unresolved = [r.text for r in reqs if not r.slugs][:MAX_UNRESOLVED_EMBED]

    # "PostgreSQL or MySQL" is covered when any of its terms is, at the best evidence level found
    weight_of: dict[str, dict[int, float]] = {}
    bm25: dict[int, float] = {}
    for slug in resolved:
        strong, used, weak = index.bitmaps(slug)
        w: dict[int, float] = {}
        if vocab.by_slug[slug].kind == "topic":
            for d in weak:
                w[d] = WEIGHT["weak"]
        for d in strong:
            w[d] = WEIGHT["strong"]
        for d in used:
            w[d] = WEIGHT["used"]
        weight_of[slug] = w
        for d, (_, b, _) in index.member_scores(slug).items():
            if b > bm25.get(d, 0.0):
                bm25[d] = b
    resolved_reqs = [r for r in reqs if r.slugs]
    n = len(resolved_reqs)
    cover_sum: dict[int, float] = {}
    cover_count: dict[int, int] = {}
    for r in resolved_reqs:
        best: dict[int, float] = {}
        for slug in r.slugs:
            for d, wt in weight_of[slug].items():
                if wt > best.get(d, 0.0):
                    best[d] = wt
        for d, wt in best.items():
            cover_sum[d] = cover_sum.get(d, 0.0) + wt
            cover_count[d] = cover_count.get(d, 0) + 1
    cover = {d: s / n for d, s in cover_sum.items()} if n else {}
    strong_members = BitMap(d for d, c in cover_count.items() if n and c / n >= min_cover)

    emb = QueryEmbedder(index.meta.get("embedder") or cfg.index.embedder, index.dim, cfg.index.cache, cfg.query.serve_socket)
    vecs = emb.embed_many([text[:8000], *unresolved])
    sql = f"SELECT doc_no, max(array_cosine_similarity(emb, $1::FLOAT[{index.dim}])) FROM chunks WHERE section <> 'education' GROUP BY doc_no"
    cosine = {int(d): float(s) for d, s in index.con.execute(sql, [vecs[0]]).fetchall()}
    # unresolved lines decide membership only for a prose-only JD: otherwise a generic intro sentence admits hundreds
    unresolved_hits = BitMap()
    if not resolved_reqs:
        for v in vecs[1:]:
            rows = index.con.execute(sql + " HAVING max(array_cosine_similarity(emb, $1::FLOAT[%d])) >= $2" % index.dim, [v, cfg.query.tau_text]).fetchall()
            unresolved_hits |= BitMap(int(d) for d, _ in rows)
    members = (strong_members | unresolved_hits) & index.all_docs
    return LikeResult(source, reqs, resolved, unresolved, min_cover, members, strong_members & members, cover, cosine, bm25)
