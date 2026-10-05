"""Add one CV and remove it again, on copies of the corpus and the index."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from agentic_search import config as config_mod
from agentic_search.config import Source
from agentic_search.index.incremental import remove_document, watch_once
from agentic_search.index.store import current_path
from agentic_search.query import verbs
from agentic_search.query.search import Criteria
from agentic_search.query.verbs import Context
from tests.testroot import TEST_ROOT, TIME_FACTOR

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
pytestmark = [pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built"),
              pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="a new CV is extracted and embedded")]

NEW_CV = """---
title: Senior Zig Systems Engineer
seniority: senior
years: 8
rate: 95
currency: EUR
location: Tallinn, Estonia
remote: true
---
# Senior Zig Systems Engineer

## Summary
Systems engineer building low-latency services in Elixir and Rust for eight years, with a focus on data pipelines.

## Skills
Elixir, Rust, Kafka, PostgreSQL, Docker

## Experience
### Senior Systems Engineer
*Northwind Labs — Tallinn, Estonia · Jan 2019 to Current*
- Built streaming data pipelines in Elixir that moved billing events from Kafka into PostgreSQL nightly.
- Led a three-person team delivering Rust services with 99.99 % availability.

## Education
MSc Computer Science, Tallinn University of Technology, 2016
"""


@pytest.fixture
def scratch(tmp_path):
    """Copies of corpus/ and index/current, an inbox with one new CV, sessions in tmp."""
    cfg = config_mod.load(TEST_ROOT)
    corpus = tmp_path / "corpus"
    shutil.copytree(cfg.corpus_out, corpus)
    index = tmp_path / "index"
    index.mkdir()
    src = current_path(cfg.index.out)
    shutil.copy(src, index / src.name)
    (index / "current").symlink_to(src.name)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "zig-engineer.md").write_text(NEW_CV, encoding="utf-8")
    cfg.corpus_out = corpus
    cfg.index.out = index
    cfg.sources = [Source(id="inbox", loader="markdown_dir", path=inbox, optional=True)]
    cfg.query.sessions = tmp_path / "sessions"
    return cfg


def test_add_one_cv_is_searchable_within_30s_and_removable(scratch):
    t0 = time.perf_counter()
    r = watch_once(scratch, extractor="none", log=lambda *_: None)
    secs = time.perf_counter() - t0
    assert r["added"] == 1 and r["ids"][0].startswith("r") and secs < 30 * TIME_FACTOR, (r, secs)
    ctx = Context(scratch, session_id="t")
    assert ctx.index.version == r["index_version"] and int(ctx.index.meta["n_docs"]) == 3048
    new_no = ctx.index.doc(r["ids"][0]).doc_no
    out = verbs.search(ctx, Criteria(skills=["elixir"], topics=["data pipelines"]))
    assert new_no in set(ctx.session().get(out.data["set"]).order), "the new CV is in elixir ∩ data-pipelines"
    used = ctx.index.bitmaps("elixir")[1]
    assert new_no in used, "Elixir in a job bullet → used level"
    assert new_no in ctx.index.bitmaps("data-pipelines")[0], "the phrase 'data pipelines' in experience → strong"
    card = ctx.index.cards([new_no])[new_no]
    assert card.startswith(r["ids"][0]) and "Elixir" in card
    assert watch_once(scratch, extractor="none", log=lambda *_: None)["added"] == 0, "a second tick adds nothing"
    # remove: gone from the bitmaps, pages say so
    rr = remove_document(scratch, r["ids"][0])
    ctx2 = Context(scratch, session_id="t")
    assert rr["removed"] == new_no and new_no not in ctx2.index.bitmaps("elixir")[0] and new_no not in ctx2.index.all_docs
    assert "removed from the index" in verbs.top(ctx2, 26).text, "the page shows the gap instead of hiding it"
