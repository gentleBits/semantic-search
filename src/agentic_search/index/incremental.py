"""`resumes index build --only-changed` and `index remove ID`.

Copy the current index file, append the new documents (every per-document step of the full build), rebuild
the two FTS indexes, and extend the term bitmaps and member_scores for the new documents only:

    strong  = doc_terms (dictionary + LLM skills + implied) ∪ phrase hits of a term's unambiguous forms in the
              new experience chunks (a regex stand-in for the build's FTS+phrase check; bm25 ≈ hit count)
    weak    = cosine(term description, new chunk) ≥ τ, minus strong
    topics fold in their implying skills' strong evidence, as in the full build.

Then swap `index/current` atomically; readers keep the old file until they exit.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
from pyroaring import BitMap

from ..config import Config
from ..ingest.markdown_dir import split_front_matter
from ..vocab.resolve import Vocabulary
from .build import BuildDoc, apply_closure, fallback_did, render_card, _write_chunks, _write_doc_terms, _write_docs
from .chunk import Chunk, chunk_document
from .embed import make_embedder
from .postings import EVIDENCE_RANK, MIN_FTS_FORM_LEN, _fts_words, _phrase_regex
from .store import current_path, swap_current
from ..extract.deterministic import extract


def _new_docs(cfg: Config, con: duckdb.DuckDBPyConnection) -> list[BuildDoc]:
    have = {r[0] for r in con.execute("SELECT doc_no FROM docs").fetchall()}
    docs = []
    for p in sorted((cfg.corpus_out / "md").glob("*.md")):
        fm, body = split_front_matter(p.read_text(encoding="utf-8"))
        if int(fm["doc_no"]) not in have:
            docs.append(BuildDoc(doc_no=int(fm["doc_no"]), id=fm["id"], fm=fm, body=body, path=p))
    return docs


def add_documents(cfg: Config, *, extractor: str | None = None, log=print) -> dict:
    icfg = cfg.index
    src = current_path(icfg.out)
    if src is None:
        raise FileNotFoundError("INDEX_NOT_BUILT: run `resumes index build` first")
    ro = duckdb.connect(str(src), read_only=True)
    docs = _new_docs(cfg, ro)
    meta = dict(ro.execute("SELECT key, value FROM meta").fetchall())
    ro.close()
    if not docs:
        return {"added": 0}
    extractor = extractor or icfg.extractor
    vocab = Vocabulary.load(icfg.vocab)
    today = datetime.now().date()
    for d in docs:
        d.det = extract(d.body, vocab, today)
        d.terms = {t.slug: t for t in d.det.terms}
    if extractor != "none":
        from ..extract.llm import run_llm_extraction

        run_llm_extraction(docs, vocab, cfg, extractor, log=log)
    for d in docs:
        apply_closure(d.terms, vocab)
    embedder = make_embedder(meta.get("embedder", icfg.embedder), int(meta.get("dim", icfg.dim)), icfg.cache)
    count = embedder.count_tokens
    for d in docs:
        d.chunks = chunk_document(d.doc_no, d.body, count, icfg.chunk_max_tokens, icfg.chunk_overlap)
        summary = (d.llm or {}).get("summary")
        if summary:
            header = f"{d.det.headline or 'Resume'} | Summary (extracted)"
            d.chunks.append(Chunk(d.doc_no, len(d.chunks), "summary", header, summary, count(header) + 1 + count(summary)))
        d.card = render_card(d, vocab, cfg, count)
        d.card_tokens = count(d.card)
    vecs = embedder.embed([c.embed_text for d in docs for c in d.chunks])

    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    tmp = icfg.out / f"resumes-{version}.duckdb.tmp"
    shutil.copy(src, tmp)
    con = duckdb.connect(str(tmp))
    try:
        start = con.execute("SELECT coalesce(max(chunk_id), -1) + 1 FROM chunks").fetchone()[0]
        _write_docs(con, docs, cfg, cfg.corpus_out.resolve())
        _write_doc_terms(con, docs, vocab, icfg.cache)
        _write_chunks_from(con, docs, vecs, icfg.cache, int(meta.get("dim", icfg.dim)), start)
        con.executemany("INSERT INTO cards VALUES (?, ?, ?)", [(d.doc_no, d.card, d.card_tokens) for d in docs])
        con.execute("INSTALL fts; LOAD fts;")
        con.execute("PRAGMA create_fts_index('chunks', 'chunk_id', 'header', 'text', stemmer='porter', overwrite=1)")
        con.execute("PRAGMA create_fts_index('docs', 'doc_no', 'markdown', stemmer='porter', overwrite=1)")
        _extend_postings(con, docs, vecs, vocab, float(meta.get("tau", icfg.tau)))
        n_docs = con.execute("SELECT count(*) FROM docs").fetchone()[0]
        n_chunks = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
        for k, v in {"index_version": version, "n_docs": str(n_docs), "n_chunks": str(n_chunks),
                     "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "last_incremental_add": ",".join(d.id for d in docs)}.items():
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [k, v])
    finally:
        con.close()
    out = icfg.out / f"resumes-{version}.duckdb"
    tmp.replace(out)
    swap_current(icfg.out, out)
    return {"added": len(docs), "ids": [d.id for d in docs], "index_version": version, "chunks": sum(len(d.chunks) for d in docs)}


def _write_chunks_from(con, docs, vecs, tmp_dir: Path, dim: int, start: int) -> None:
    from .store import bulk_insert

    rows, i = [], 0
    for d in docs:
        for c in d.chunks:
            rows.append({"chunk_id": start + i, "doc_no": d.doc_no, "idx": c.idx, "section": c.section, "header": c.header,
                         "text": c.text, "tokens": c.tokens, "emb": vecs[i]})
            i += 1
    bulk_insert(con, "chunks", f"chunk_id, doc_no, idx, section, header, text, tokens, emb::FLOAT[{dim}]", rows, tmp_dir)


def _extend_postings(con, docs: list[BuildDoc], vecs, vocab: Vocabulary, tau: float) -> None:
    term_rows = con.execute("SELECT term_id, slug, kind, emb FROM terms ORDER BY term_id").fetchall()
    T = np.stack([np.asarray(r[3], dtype=np.float32) for r in term_rows])
    C = np.stack([np.asarray(v, dtype=np.float32) for v in vecs]) if vecs else np.zeros((0, T.shape[1]), dtype=np.float32)
    chunk_doc, chunk_ok, chunk_text = [], [], []
    for d in docs:
        for c in d.chunks:
            chunk_doc.append(d.doc_no)
            chunk_ok.append(c.section != "education")
            chunk_text.append(c.text if c.section == "experience" else "")
    chunk_doc = np.array(chunk_doc)
    S = T @ C.T if len(C) else np.zeros((len(T), 0))
    id_of = {r[1]: r[0] for r in term_rows}
    topic_ids = {r[0] for r in term_rows if r[2] == "topic"}
    ev: dict[int, dict[int, int]] = {}
    llm_topic: dict[int, dict[int, int]] = {}
    for d in docs:
        for slug, t in d.terms.items():
            tid = id_of.get(slug)
            if tid is None:
                continue
            target = llm_topic if (t.via == "llm" and tid in topic_ids) else ev
            target.setdefault(tid, {})[d.doc_no] = max(target.get(tid, {}).get(d.doc_no, 0), EVIDENCE_RANK.get(t.level, 1))
    bm: dict[int, dict[int, float]] = {}
    for t in vocab.terms:
        for form in t.unambiguous_forms:
            words = _fts_words(form)
            if not words or (len(words) == 1 and len(words[0]) < MIN_FTS_FORM_LEN):
                continue
            rx = _phrase_regex(words)
            for ci, text in enumerate(chunk_text):
                if text and rx.search(text):
                    dn = int(chunk_doc[ci])
                    bm.setdefault(t.term_id, {})[dn] = bm.get(t.term_id, {}).get(dn, 0.0) + 1.0
    cos: dict[int, dict[int, float]] = {}
    for i, (tid, _, _, _) in enumerate(term_rows):
        if S.shape[1] == 0:
            continue
        for ci in np.nonzero((S[i] >= tau) & np.array(chunk_ok))[0]:
            dn = int(chunk_doc[ci])
            cos.setdefault(tid, {})[dn] = max(cos.get(tid, {}).get(dn, 0.0), float(S[i, ci]))
    for t in vocab.terms:
        for s in vocab.implying(t.slug):
            for dn, r in ev.get(id_of[s], {}).items():
                ev.setdefault(t.term_id, {})[dn] = max(ev.get(t.term_id, {}).get(dn, 0), r)
            for dn, v in bm.get(id_of[s], {}).items():
                bm.setdefault(t.term_id, {})[dn] = max(bm.get(t.term_id, {}).get(dn, 0.0), v)
    rows = []
    for tid, s_bm, u_bm, w_bm in con.execute("SELECT term_id, strong_bm, used_bm, weak_bm FROM postings").fetchall():
        e, b, c = ev.get(tid, {}), bm.get(tid, {}), cos.get(tid, {})
        strong = set(e) | set(b)
        if not strong and not c:
            continue
        for dn, r in llm_topic.get(tid, {}).items():
            if dn in strong:
                e[dn] = max(e.get(dn, 0), r)
        used = {dn for dn, r in e.items() if r >= 2}
        weak = set(c) - strong
        S_, U_, W_ = BitMap.deserialize(bytes(s_bm)), BitMap.deserialize(bytes(u_bm)), BitMap.deserialize(bytes(w_bm))
        S_ |= BitMap(strong); U_ |= BitMap(used); W_ = (W_ | BitMap(weak)) - S_
        con.execute("UPDATE postings SET strong_bm = ?, used_bm = ?, weak_bm = ?, n_strong = ?, n_used = ?, n_weak = ? WHERE term_id = ?",
                    [S_.serialize(), U_.serialize(), W_.serialize(), len(S_), len(U_), len(W_), tid])
        for dn in strong | weak:
            rows.append((tid, dn, e.get(dn, 0), b.get(dn, 0.0), c.get(dn, 0.0)))
    if rows:
        con.executemany("INSERT OR REPLACE INTO member_scores VALUES (?, ?, ?, ?, ?)", rows)


def remove_document(cfg: Config, ref: str) -> dict:
    """`index remove ID`: mark deleted, drop it from every bitmap and from member_scores, swap."""
    icfg = cfg.index
    src = current_path(icfg.out)
    if src is None:
        raise FileNotFoundError("INDEX_NOT_BUILT")
    ro = duckdb.connect(str(src), read_only=True)
    row = ro.execute("SELECT doc_no FROM docs WHERE id = ? OR (? AND doc_no = ?)", [ref, ref.isdigit(), int(ref) if ref.isdigit() else -1]).fetchone()
    ro.close()
    if row is None:
        raise KeyError(f"UNKNOWN_DOC_ID {ref}")
    doc_no = row[0]
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    tmp = icfg.out / f"resumes-{version}.duckdb.tmp"
    shutil.copy(src, tmp)
    con = duckdb.connect(str(tmp))
    try:
        con.execute("UPDATE docs SET deleted = true WHERE doc_no = ?", [doc_no])
        con.execute("DELETE FROM member_scores WHERE doc_no = ?", [doc_no])
        for tid, s_bm, u_bm, w_bm in con.execute("SELECT term_id, strong_bm, used_bm, weak_bm FROM postings").fetchall():
            S_, U_, W_ = (BitMap.deserialize(bytes(b)) for b in (s_bm, u_bm, w_bm))
            if doc_no in S_ or doc_no in W_:
                S_.discard(doc_no); U_.discard(doc_no); W_.discard(doc_no)
                con.execute("UPDATE postings SET strong_bm = ?, used_bm = ?, weak_bm = ?, n_strong = ?, n_used = ?, n_weak = ? WHERE term_id = ?",
                            [S_.serialize(), U_.serialize(), W_.serialize(), len(S_), len(U_), len(W_), tid])
        con.execute("INSERT OR REPLACE INTO meta VALUES ('index_version', ?)", [version])
    finally:
        con.close()
    out = icfg.out / f"resumes-{version}.duckdb"
    tmp.replace(out)
    swap_current(icfg.out, out)
    return {"removed": doc_no, "index_version": version}


def watch_once(cfg: Config, *, extractor: str | None = None, log=print) -> dict:
    """One tick of `resumes watch`: convert new inbox files, then add them to the index."""
    from ..ingest.add import add_new

    ids = add_new(cfg, log=log)
    if not ids:
        return {"added": 0}
    return add_documents(cfg, extractor=extractor, log=log)
