"""The built index: tables, chunks, postings, cards, profiles and fixture quality (skipped when not built)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pyroaring import BitMap

from agentic_search import config as config_mod
from agentic_search.index.embed import token_counter
from agentic_search.index.eval_fixture import evaluate
from agentic_search.index.store import current_path, open_current, read_meta
from tests.testroot import TEST_ROOT

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
INDEX_DIR = CFG.index.out

pytestmark = pytest.mark.skipif(current_path(INDEX_DIR) is None, reason="index not built")


@pytest.fixture(scope="module")
def con():
    c = open_current(INDEX_DIR)
    c.execute("LOAD fts;")
    return c


@pytest.fixture(scope="module")
def meta(con):
    return read_meta(con)


def test_current_symlink_and_meta(con, meta):
    p = current_path(INDEX_DIR)
    assert p.is_file() and re.match(r"resumes-\d{8}T\d{6}\.duckdb$", p.name)
    assert meta["embedder"] == CFG.index.embedder and int(meta["dim"]) == CFG.index.dim
    assert meta["schema_version"] == "1" and meta["index_version"] in p.name


def test_counts(con, meta):
    n_docs = con.execute("SELECT count(*) FROM docs").fetchone()[0]
    assert n_docs == int(meta["n_docs"]) == 3047
    assert con.execute("SELECT count(*) FROM profile").fetchone()[0] == n_docs
    assert con.execute("SELECT count(*) FROM cards").fetchone()[0] == n_docs
    n_chunks = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert n_chunks == int(meta["n_chunks"]) and n_chunks >= n_docs
    assert con.execute("SELECT count(DISTINCT doc_no) FROM chunks").fetchone()[0] == n_docs, "every document has at least one chunk"
    n_terms = con.execute("SELECT count(*) FROM terms").fetchone()[0]
    assert n_terms == int(meta["n_terms"]) == con.execute("SELECT count(*) FROM postings").fetchone()[0]


def test_doc_numbers_match_corpus_registry(con):
    reg = json.loads((CFG.corpus_out / "registry.json").read_text(encoding="utf-8"))
    expected = {e["doc_no"] for e in reg["docs"] if not e.get("deleted")}
    assert {r[0] for r in con.execute("SELECT doc_no FROM docs").fetchall()} == expected


def test_chunks_within_budget_and_embedded(con):
    mx = con.execute("SELECT max(tokens) FROM chunks").fetchone()[0]
    assert mx <= CFG.index.chunk_max_tokens
    row = con.execute("SELECT emb FROM chunks LIMIT 1").fetchone()[0]
    assert len(row) == CFG.index.dim
    norm = con.execute("SELECT sqrt(list_sum(list_transform(emb::FLOAT[], x -> x*x))) FROM chunks LIMIT 1").fetchone()[0]
    assert abs(norm - 1.0) < 0.01, "embeddings are unit vectors (cosine = dot)"


def test_fts_and_cosine_queries_work(con):
    hits = con.execute(
        "SELECT count(*) FROM (SELECT fts_main_chunks.match_bm25(chunk_id, 'kubernetes', conjunctive := 1) AS s FROM chunks) WHERE s IS NOT NULL"
    ).fetchone()[0]
    assert hits > 0
    top = con.execute(
        "SELECT c.header FROM chunks c, (SELECT emb FROM terms WHERE slug = 'data-pipelines') t "
        "ORDER BY array_cosine_similarity(c.emb, t.emb) DESC LIMIT 5"
    ).fetchall()
    assert len(top) == 5


def test_postings_are_consistent(con):
    rows = con.execute("SELECT term_id, strong_bm, used_bm, weak_bm, n_strong, n_used, n_weak FROM postings").fetchall()
    ms = dict(con.execute("SELECT term_id, count(*) FROM member_scores GROUP BY term_id").fetchall())
    for term_id, s, u, w, ns, nu, nw in rows:
        S, U, W = BitMap.deserialize(bytes(s)), BitMap.deserialize(bytes(u)), BitMap.deserialize(bytes(w))
        assert (len(S), len(U), len(W)) == (ns, nu, nw)
        assert not (S & W), "weak is disjoint from strong"
        assert U <= S, "used ⊆ strong"
        assert ms.get(term_id, 0) == len(S) + len(W), "one member_scores row per member"


def test_every_doc_term_has_a_valid_term_and_level(con):
    bad = con.execute(
        "SELECT count(*) FROM doc_terms dt LEFT JOIN terms t USING (term_id) WHERE t.term_id IS NULL OR dt.level NOT IN ('listed','used','led')"
    ).fetchone()[0]
    assert bad == 0
    docs_with_terms = con.execute("SELECT count(DISTINCT doc_no) FROM doc_terms").fetchone()[0]
    assert docs_with_terms >= 0.98 * 3047


def test_cards(con):
    count = token_counter(CFG.index.embedder)
    over = 0
    for doc_no, doc_id, text, tokens in con.execute("SELECT c.doc_no, d.id, c.text, c.tokens FROM cards c JOIN docs d USING (doc_no)").fetchall():
        assert text.startswith(doc_id + " · "), doc_id
        assert "file://" not in text, doc_id  # the link depends on the machine; it is added when a card is shown
        assert tokens == count(text)
        over += tokens > CFG.index.card_max_tokens
    assert over == 0, f"{over} cards over the {CFG.index.card_max_tokens}-token cap"
    avg = con.execute("SELECT avg(tokens) FROM cards").fetchone()[0]
    assert 35 <= avg <= 110  # without the link line (about 20 tokens), as stored since links depend on the machine


def test_profile_coverage(con):
    years, edu, title = con.execute(
        "SELECT count(*) FILTER (WHERE years IS NOT NULL), count(*) FILTER (WHERE education IS NOT NULL), "
        "count(*) FILTER (WHERE current_title IS NOT NULL) FROM profile"
    ).fetchone()
    assert years >= 0.9 * 3047 and edu >= 0.9 * 3047 and title >= 0.98 * 3047
    rate = con.execute("SELECT count(*) FROM profile WHERE rate IS NULL OR currency <> 'EUR'").fetchone()[0]
    assert rate == 0


def test_fixture_membership_quality(con):
    r = evaluate(CFG, con)
    dp = r["data-pipelines/any"]
    assert dp["precision"] >= 0.9 and dp["recall"] >= 0.85, dp
    el = r["elixir/strong"]
    assert el["precision"] == 1.0 and el["recall"] == 1.0, el
    assert el["negative_controls_included"] == 0 and el["phoenix_only_found"] == el["phoenix_only_total"]
    assert r["B_in_rest_apis"] is True
    assert r["E_in_java_used"] is False and r["E_java_level"] == "listed"
