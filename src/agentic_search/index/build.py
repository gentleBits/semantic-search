"""`resumes index build`.

corpus/md → deterministic extraction → (LLM extraction, cached) → term resolution + closure →
chunks → embeddings (cached) → DuckDB file (docs, profile, terms, doc_terms, chunks + FTS, cards, meta)
→ postings bitmaps + member_scores → smoke query → atomic swap of index/current.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from ..config import Config
from ..extract.deterministic import DocTerm, extract
from ..extract.sections import parse
from ..ingest.markdown_dir import split_front_matter
from ..vocab.resolve import Vocabulary
from . import cards as cards_mod
from .chunk import Chunk, chunk_document
from .embed import make_embedder
from .store import SCHEMA_VERSION, bulk_insert, ddl, swap_current

LEVEL_RANK = {"listed": 1, "used": 2, "led": 3}


@dataclass
class BuildDoc:
    doc_no: int
    id: str
    fm: dict
    body: str
    path: Path
    det: object = None                   # DetProfile
    llm: dict | None = None              # merged LLM extraction (see extract/llm.py)
    terms: dict[str, DocTerm] = field(default_factory=dict)   # slug → best DocTerm (after closure)
    chunks: list = field(default_factory=list)
    card: str = ""
    card_tokens: int = 0
    summary: str | None = None


class Timer:
    def __init__(self, log) -> None:
        self.log = log
        self.t0 = time.time()
        self.marks: dict[str, float] = {}

    def mark(self, name: str) -> None:
        now = time.time()
        self.marks[name] = round(now - self.t0, 1)
        self.log(f"  [{self.marks[name]:6.1f}s] {name}")
        self.t0 = now


def load_corpus(cfg: Config) -> list[BuildDoc]:
    md_dir = cfg.corpus_out / "md"
    docs: list[BuildDoc] = []
    for p in sorted(md_dir.glob("*.md")):
        fm, body = split_front_matter(p.read_text(encoding="utf-8"))
        docs.append(BuildDoc(doc_no=int(fm["doc_no"]), id=fm["id"], fm=fm, body=body, path=p))
    if not docs:
        raise FileNotFoundError(f"CORPUS_NOT_BUILT: no files in {md_dir}")
    return docs


def apply_closure(terms: dict[str, DocTerm], vocab: Vocabulary) -> None:
    """A document tagged with t is also tagged with everything t implies, at the same level, via='implied'."""
    for slug, dt in list(terms.items()):
        for imp in vocab.implied(slug):
            cur = terms.get(imp)
            if cur is None or LEVEL_RANK[dt.level] > LEVEL_RANK[cur.level]:
                terms[imp] = DocTerm(imp, dt.level, "implied", dt.evidence, dt.section)


def build(cfg: Config, *, extractor: str | None = None, log=print, seen: dict | None = None) -> dict:
    icfg = cfg.index
    assert icfg is not None
    timer = Timer(log)
    today = date.today()
    extractor = extractor or icfg.extractor
    vocab = Vocabulary.load(icfg.vocab)
    docs = load_corpus(cfg)
    log(f"index build: {len(docs)} documents, embedder {icfg.embedder}, extractor {extractor}")
    timer.mark("load corpus")

    for d in docs:
        d.det = extract(d.body, vocab, today)
        d.terms = {t.slug: t for t in d.det.terms}
    timer.mark("deterministic extraction")

    llm_stats: dict = {"enabled": False}
    if extractor != "none":
        from ..extract.llm import run_llm_extraction

        llm_stats = run_llm_extraction(docs, vocab, cfg, extractor, log=log, seen=seen)
        timer.mark("llm extraction")

    for d in docs:
        apply_closure(d.terms, vocab)
    timer.mark("term closure")

    embedder = make_embedder(icfg.embedder, icfg.dim, icfg.cache)
    count = embedder.count_tokens
    for d in docs:
        d.chunks = chunk_document(d.doc_no, d.body, count, icfg.chunk_max_tokens, icfg.chunk_overlap)
        summary = (d.llm or {}).get("summary")
        if summary:  # the extracted summary is a dense, retrieval-friendly chunk of its own
            header = f"{d.det.headline or 'Resume'} | Summary (extracted)"
            d.chunks.append(Chunk(d.doc_no, len(d.chunks), "summary", header, summary, count(header) + 1 + count(summary)))
    n_chunks = sum(len(d.chunks) for d in docs)
    timer.mark(f"chunking ({n_chunks} chunks)")

    chunk_texts = [c.embed_text for d in docs for c in d.chunks]
    chunk_vecs = embedder.embed(chunk_texts, log=log)
    term_vecs = embedder.embed([t.description for t in vocab.terms])
    if seen is not None:
        seen["keys"] |= embedder.seen
    timer.mark(f"embeddings (cache hits {embedder.hits}, misses {embedder.misses})")

    corpus_root = cfg.corpus_out.resolve()
    over_cap = 0
    for d in docs:
        d.card = render_card(d, vocab, cfg, count)
        d.card_tokens = count(d.card)
        if d.card_tokens > icfg.card_max_tokens:
            over_cap += 1
    timer.mark(f"cards ({over_cap} over cap)")

    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    icfg.out.mkdir(parents=True, exist_ok=True)
    out_file = icfg.out / f"resumes-{version}.duckdb"
    tmp_file = icfg.out / f"resumes-{version}.duckdb.tmp"
    if tmp_file.exists():
        tmp_file.unlink()
    con = duckdb.connect(str(tmp_file))
    con.execute(ddl(icfg.dim))
    _write_docs(con, docs, cfg, corpus_root)
    _write_terms(con, vocab, term_vecs, icfg.cache, icfg.dim)
    _write_doc_terms(con, docs, vocab, icfg.cache)
    _write_chunks(con, docs, chunk_vecs, icfg.cache, icfg.dim)
    con.executemany("INSERT INTO cards VALUES (?, ?, ?)", [(d.doc_no, d.card, d.card_tokens) for d in docs])
    timer.mark("write tables")

    con.execute("INSTALL fts; LOAD fts;")
    con.execute("PRAGMA create_fts_index('chunks', 'chunk_id', 'header', 'text', stemmer='porter', overwrite=1)")
    con.execute("PRAGMA create_fts_index('docs', 'doc_no', 'markdown', stemmer='porter', overwrite=1)")
    timer.mark("fts indexes")

    from .postings import build_postings

    post_stats = build_postings(con, vocab, icfg, log=log)
    timer.mark("postings + member_scores")

    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "index_version": version,
        "embedder": embedder.model_id,
        "dim": str(icfg.dim),
        "extractor": extractor,
        "tau": str(icfg.tau),
        "n_docs": str(len(docs)),
        "n_chunks": str(n_chunks),
        "n_terms": str(len(vocab.terms)),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vocab_hash": hashlib.sha256(icfg.vocab.read_bytes()).hexdigest()[:12],
        "llm_extracted": str(llm_stats.get("extracted", 0)),
        "cards_over_cap": str(over_cap),
    }
    con.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
    con.close()

    check = duckdb.connect(str(tmp_file), read_only=True)
    check.execute("LOAD fts;")
    n = check.execute("SELECT count(*) FROM docs").fetchone()[0]
    fts_hit = check.execute(
        "SELECT count(*) FROM (SELECT fts_main_chunks.match_bm25(chunk_id, 'engineer') AS s FROM chunks) WHERE s IS NOT NULL"
    ).fetchone()[0]
    check.close()
    if n != len(docs) or fts_hit == 0:
        raise RuntimeError(f"smoke query failed: docs={n}, fts_hits={fts_hit}")
    tmp_file.replace(out_file)
    swap_current(icfg.out, out_file)
    _prune_old(icfg.out, keep=2)
    timer.mark("smoke query + swap")

    stats = {
        "index_version": version,
        "file": str(out_file),
        "docs": len(docs),
        "chunks": n_chunks,
        "terms": len(vocab.terms),
        "cards_over_cap": over_cap,
        "embedding_cache": {"hits": embedder.hits, "misses": embedder.misses},
        "llm": llm_stats,
        "postings": post_stats,
        "timings_s": timer.marks,
        "total_s": round(sum(timer.marks.values()), 1),
    }
    (icfg.out / "build-stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    return stats


def render_card(d: BuildDoc, vocab: Vocabulary, cfg: Config, count) -> str:
    fm, det, llm = d.fm, d.det, d.llm or {}
    title = llm.get("current_title") or fm.get("title") or det.current_title or det.headline or "Resume"
    years = det.years if det.years is not None else llm.get("years_experience")
    skills = [
        (vocab.by_slug[s].canonical, t.level)
        for s, t in d.terms.items()
        if vocab.by_slug[s].kind == "skill" and t.via != "implied"
    ]
    did = llm.get("did") or fallback_did(d.body)
    ci = cards_mod.CardInput(
        id=d.id,
        title=title,
        years=years,
        rate=fm.get("rate"),
        currency=fm.get("currency") or cfg.currency,
        location=llm.get("location") or fm.get("location"),
        remote=llm.get("remote") if llm.get("remote") is not None else fm.get("remote"),
        availability=llm.get("availability") or fm.get("availability"),
        skills=skills,
        did=did,
    )
    return cards_mod.render(ci, count, cfg.index.card_max_tokens)


def fallback_did(body: str) -> str | None:
    """First bullet of the most recent job, cut to 110 chars."""
    doc = parse(body)
    for e in doc.experience_entries:
        for line in e.lines:
            if len(line.split()) >= 4:
                return line[:110].rstrip() + ("…" if len(line) > 110 else "")
    return None


def _write_docs(con, docs: list[BuildDoc], cfg: Config, corpus_root: Path) -> None:
    now = datetime.now(timezone.utc)
    con.executemany(
        "INSERT INTO docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, false, ?)",
        [
            (d.doc_no, d.id, d.fm["hash"], d.fm["source"], str(d.fm["source_id"]), d.fm.get("category"),
             str(d.path.relative_to(cfg.root)) if d.path.is_relative_to(cfg.root) else str(d.path), d.body, d.fm["words"], now, d.fm.get("dup_group"))
            for d in docs
        ],
    )
    rows = []
    for d in docs:
        fm, det, llm = d.fm, d.det, d.llm or {}
        years = det.years if det.years is not None else llm.get("years_experience")
        years_source = det.years_source if det.years is not None else ("llm" if llm.get("years_experience") is not None else None)
        did = llm.get("did") or fallback_did(d.body)
        rows.append((
            d.doc_no, det.headline, llm.get("current_title") or fm.get("title") or det.current_title,
            llm.get("seniority") or fm.get("seniority") or fm.get("seniority_hint"),
            years, years_source, fm.get("rate"), fm.get("currency") or cfg.currency, fm.get("rate_source"),
            llm.get("location") or fm.get("location"),
            llm.get("remote") if llm.get("remote") is not None else fm.get("remote"),
            llm.get("availability") or fm.get("availability"),
            det.education or llm.get("education"),
            did, "llm" if llm.get("did") else ("fallback" if did else None),
            llm.get("summary"), llm.get("model"), llm.get("extracted_at"),
        ))
    con.executemany("INSERT INTO profile VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _write_terms(con, vocab: Vocabulary, term_vecs: list[list[float]], tmp_dir: Path, dim: int) -> None:
    rows = [
        {"term_id": t.term_id, "kind": t.kind, "slug": t.slug, "canonical": t.canonical, "aliases": t.aliases,
         "implies": t.implies, "ambiguous": sorted(t.ambiguous), "description": t.description, "emb": v}
        for t, v in zip(vocab.terms, term_vecs)
    ]
    bulk_insert(con, "terms", f"term_id, kind, slug, canonical, aliases::VARCHAR[], implies::VARCHAR[], ambiguous::VARCHAR[], description, emb::FLOAT[{dim}]", rows, tmp_dir)


def _write_doc_terms(con, docs: list[BuildDoc], vocab: Vocabulary, tmp_dir: Path) -> None:
    rows = [
        {"doc_no": d.doc_no, "term_id": vocab.by_slug[s].term_id, "level": t.level, "via": t.via,
         "evidence": (t.evidence or "")[:300], "section": t.section}
        for d in docs
        for s, t in d.terms.items()
    ]
    bulk_insert(con, "doc_terms", "doc_no, term_id, level, via, evidence, section", rows, tmp_dir)


def _write_chunks(con, docs: list[BuildDoc], chunk_vecs: list[list[float]], tmp_dir: Path, dim: int) -> None:
    rows = []
    i = 0
    for d in docs:
        for c in d.chunks:
            rows.append({"chunk_id": i, "doc_no": d.doc_no, "idx": c.idx, "section": c.section, "header": c.header,
                         "text": c.text, "tokens": c.tokens, "emb": chunk_vecs[i]})
            i += 1
    bulk_insert(con, "chunks", f"chunk_id, doc_no, idx, section, header, text, tokens, emb::FLOAT[{dim}]", rows, tmp_dir)


def _prune_old(index_dir: Path, keep: int = 2) -> None:
    files = sorted(index_dir.glob("resumes-*.duckdb"), key=lambda p: p.name)
    current = (index_dir / "current").resolve() if (index_dir / "current").exists() else None
    for p in files[:-keep]:
        if current is None or p.resolve() != current:
            p.unlink()
