"""Term membership, decided once at build time.

    strong_bm[t] = doc_terms(t)                       LLM (quote-checked) + dictionary (ambiguity rule) + implied
                 ∪ FTS hits for t's unambiguous forms in experience chunks (stemmed variants)
    weak_bm[t]   = docs with a chunk: cosine(q_t, chunk) ≥ τ, minus strong    semantic only
    used_bm[t]   = strong members with level used|led
    member_scores(t, doc) = (evidence_rank, max bm25, max cosine); topics take the max over implying skills.
"""

from __future__ import annotations

import re
from collections import defaultdict

import duckdb
import numpy as np
from pyroaring import BitMap

from ..config import IndexConfig
from ..vocab.resolve import Vocabulary

EVIDENCE_RANK = {"led": 3, "used": 2, "listed": 1}
MIN_FTS_FORM_LEN = 4   # "c", "r", "go", "ad": pure noise once FTS strips punctuation and case
_WORD = re.compile(r"[a-z0-9]+")


def _fts_words(form: str) -> list[str]:
    return _WORD.findall(form.lower())


def _phrase_regex(words: list[str]) -> re.Pattern:
    """Adjacent stem-prefix phrase: 'data pipelines' → r'\\bdata\\w*\\W+pipelin\\w*' (tolerates plural/tense)."""
    parts = []
    for w in words:
        prefix = w if len(w) <= 4 else w[: max(4, len(w) - 2)]
        parts.append(re.escape(prefix) + r"\w*")
    return re.compile(r"\b" + r"\W+".join(parts), re.I)


def build_postings(con: duckdb.DuckDBPyConnection, vocab: Vocabulary, icfg: IndexConfig, log=print) -> dict:
    chunk_rows = con.execute("SELECT chunk_id, doc_no, section FROM chunks ORDER BY chunk_id").fetchall()
    chunk_doc = np.array([r[1] for r in chunk_rows], dtype=np.int32)
    chunk_section = np.array([r[2] for r in chunk_rows])
    weak_ok = chunk_section != "education"
    embs = con.execute("SELECT emb FROM chunks ORDER BY chunk_id").fetchnumpy()["emb"]
    C = np.stack([np.asarray(e, dtype=np.float32) for e in embs])          # (n_chunks, dim)
    term_rows = con.execute("SELECT term_id, slug, emb FROM terms ORDER BY term_id").fetchall()
    T = np.stack([np.asarray(r[2], dtype=np.float32) for r in term_rows])   # (n_terms, dim)
    slug_of = {r[0]: r[1] for r in term_rows}
    id_of = {r[1]: r[0] for r in term_rows}

    # ---- channel 1: doc_terms (dictionary + LLM + implied).
    # An LLM *topic* assertion alone does not make a member: the quote is real, but the model's topics are
    # broader than the vocabulary's ("Kafka event streams" → data-pipelines) and cost precision. It only
    # raises the evidence level of a document another channel already made a member.
    tagged: dict[int, dict[int, int]] = defaultdict(dict)     # term_id → {doc_no: evidence_rank}
    llm_topic: dict[int, dict[int, int]] = defaultdict(dict)  # level-only contributions
    topic_ids = {r[0] for r in con.execute("SELECT term_id FROM terms WHERE kind = 'topic'").fetchall()}
    for term_id, doc_no, level, via in con.execute("SELECT term_id, doc_no, level, via FROM doc_terms").fetchall():
        target = llm_topic if (via == "llm" and term_id in topic_ids) else tagged
        target[term_id][doc_no] = max(target[term_id].get(doc_no, 0), EVIDENCE_RANK.get(level, 1))

    # ---- channel 2: FTS over experience chunks, unambiguous forms only.
    # FTS conjunctive matching is bag-of-words ("data" + "pipeline" anywhere in the chunk), so a
    # multi-word form is only accepted when the words occur as an adjacent phrase (stem prefixes).
    fts_docs: dict[int, dict[int, float]] = defaultdict(dict)  # term_id → {doc_no: max bm25}
    n_fts_queries = 0
    chunk_text_cache: dict[int, str] = {}

    def chunk_text(cid: int) -> str:
        if cid not in chunk_text_cache:
            chunk_text_cache[cid] = con.execute("SELECT text FROM chunks WHERE chunk_id = ?", [cid]).fetchone()[0]
        return chunk_text_cache[cid]

    for t in vocab.terms:
        for form in t.unambiguous_forms:
            words = _fts_words(form)
            if not words or (len(words) == 1 and len(words[0]) < MIN_FTS_FORM_LEN):
                continue
            q = " ".join(words)
            n_fts_queries += 1
            rows = con.execute(
                "SELECT chunk_id, s FROM (SELECT chunk_id, fts_main_chunks.match_bm25(chunk_id, ?, conjunctive := 1) AS s FROM chunks WHERE section = 'experience') WHERE s IS NOT NULL",
                [q],
            ).fetchall()
            phrase = _phrase_regex(words) if len(words) > 1 else None
            for cid, s in rows:
                if phrase is not None and not phrase.search(chunk_text(cid)):
                    continue
                d = int(chunk_doc[cid])
                if s > fts_docs[t.term_id].get(d, 0.0):
                    fts_docs[t.term_id][d] = float(s)

    # ---- channel 3: cosine of term description vs chunks
    S = T @ C.T                                                  # (n_terms, n_chunks)
    S[:, ~weak_ok] = -1.0

    def doc_max(scores: np.ndarray, mask: np.ndarray) -> dict[int, float]:
        out: dict[int, float] = {}
        for ci in np.nonzero(mask)[0]:
            d = int(chunk_doc[ci])
            v = float(scores[ci])
            if v > out.get(d, -1.0):
                out[d] = v
        return out

    per_term: dict[int, tuple[dict[int, int], dict[int, float], dict[int, float]]] = {}
    for i, (term_id, slug, _) in enumerate(term_rows):
        weak = doc_max(S[i], S[i] >= icfg.tau)
        per_term[term_id] = (tagged.get(term_id, {}), fts_docs.get(term_id, {}), weak)

    # ---- topics: fold in the implying skills' STRONG evidence (dictionary/LLM levels, FTS scores).
    # Not their weak sets: a skill's generated description is a noisier query than the topic's own.
    for t in vocab.terms:
        imp = vocab.implying(t.slug)
        if not imp:
            continue
        ev, bm, cos = per_term[t.term_id]
        ev, bm = dict(ev), dict(bm)
        for s in imp:
            e2, b2, _ = per_term[id_of[s]]
            for d, v in e2.items():
                ev[d] = max(ev.get(d, 0), v)
            for d, v in b2.items():
                bm[d] = max(bm.get(d, 0.0), v)
        per_term[t.term_id] = (ev, bm, cos)

    post_rows, score_rows = [], []
    n_strong_total = n_weak_total = 0
    for term_id, (ev, bm, cos) in per_term.items():
        strong = set(ev) | set(bm)
        for d, r in llm_topic.get(term_id, {}).items():   # level only, for members found by other channels
            if d in strong:
                ev[d] = max(ev.get(d, 0), r)
        used = {d for d, r in ev.items() if r >= 2}
        weak = set(cos) - strong
        n_strong_total += len(strong)
        n_weak_total += len(weak)
        post_rows.append((
            term_id, BitMap(strong).serialize(), BitMap(used).serialize(), BitMap(weak).serialize(),
            len(strong), len(used), len(weak),
        ))
        for d in strong | weak:
            score_rows.append((term_id, d, ev.get(d, 0), bm.get(d, 0.0), cos.get(d, 0.0)))
    con.executemany("INSERT INTO postings VALUES (?, ?, ?, ?, ?, ?, ?)", post_rows)
    con.executemany("INSERT INTO member_scores VALUES (?, ?, ?, ?, ?)", score_rows)
    log(f"    postings: {len(post_rows)} terms, {n_strong_total} strong + {n_weak_total} weak memberships, {n_fts_queries} FTS queries")
    return {"terms": len(post_rows), "strong_memberships": n_strong_total, "weak_memberships": n_weak_total, "fts_queries": n_fts_queries}
