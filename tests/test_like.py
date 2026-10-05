"""`--like` on the Java job descriptions (skipped without the index or an embedder)."""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

import pytest

from agentic_search import config as config_mod
from agentic_search.errors import ResumesError
from agentic_search.index.store import current_path
from agentic_search.query import verbs
from agentic_search.query.like import parse_requirements
from agentic_search.query.search import Criteria
from agentic_search.query.verbs import Context
from tests.testroot import TEST_ROOT, TIME_FACTOR

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
JD = ROOT / "eval" / "jds" / "java-backend.md"
pytestmark = [pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built"),
              pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="--like embeds the JD")]
GT = json.loads((ROOT / "data" / "fixture" / "ground_truth.json").read_text(encoding="utf-8"))["docs"]


@pytest.fixture
def ctx(tmp_path):
    cfg = config_mod.load(TEST_ROOT)
    cfg.query.sessions = tmp_path / "sessions"
    return Context(cfg, session_id="t")


@pytest.fixture(scope="module")
def fixture_docs():
    ix = verbs.Index(config_mod.load(TEST_ROOT))
    return dict(ix.con.execute("SELECT source_id, doc_no FROM docs WHERE source = 'fixture'").fetchall())


def _specials(fixture_docs, name):
    return [fixture_docs[s] for s, g in GT.items() if g["special"] == name]


def test_parse_requirements():
    reqs = parse_requirements(JD.read_text())
    assert len(reqs) == 9 and reqs[0].startswith("We are looking") and "Kubernetes" in reqs and "Nice to have:" not in reqs, "headings and 'x:' headers are dropped; a bare term stays"


def test_java_jd_walkthrough(ctx, fixture_docs):
    t0 = time.perf_counter()
    out = verbs.search(ctx, Criteria(like=str(JD)))
    ms = (time.perf_counter() - t0) * 1000
    head = out.text.splitlines()[0]
    assert "like java-backend.md (requirements 9, resolved" in head and "cover ≥ 40%" in head
    like = out.data["like"]
    assert {"java", "spring", "hibernate", "rest-apis"} <= set(like["resolved"]) and like["unresolved"], like
    order = ctx.session().get(out.data["set"]).order
    pos = {d: i for i, d in enumerate(order)}
    full, partial = _specials(fixture_docs, "java_full"), _specials(fixture_docs, "java_partial")
    b, e, d_ = _specials(fixture_docs, "B")[0], _specials(fixture_docs, "E")[0], _specials(fixture_docs, "D")[0]
    assert all(x in pos for x in full + [b, d_]), "the full matches, B and D are members"
    assert sum(x in pos for x in partial) >= 2, "two of four core terms usually clear 40 % of a seven-line JD"
    pos.setdefault(e, len(order))       # E (Java listed only, SAP work) may fall below 40 % coverage
    assert statistics.median(pos[x] for x in full) < statistics.median(pos.get(x, len(order)) for x in partial), "all four terms rank above two of four"
    assert pos[b] < 20, f"B (web services, never REST) in the top 20 of a seven-line JD; is at {pos[b] + 1}"
    assert pos[e] > pos[b], "E lists Java/Spring/Hibernate but its work is SAP: never above B"
    assert ms < 1500 * TIME_FACTOR, f"--like took {ms:.0f} ms"


def test_core_java_jd_walkthrough(ctx, fixture_docs):
    """The four-term Java JD: B (web services, never REST) in the top 5 fixture people, E far below B."""
    out = verbs.search(ctx, Criteria(like=str(JD.with_name("java-core.md"))))
    assert out.data["like"]["resolved"] == ["java", "spring", "hibernate", "rest-apis"], out.data["like"]
    order = ctx.session().get(out.data["set"]).order
    pos = {d: i for i, d in enumerate(order)}
    b, e = _specials(fixture_docs, "B")[0], _specials(fixture_docs, "E")[0]
    full = _specials(fixture_docs, "java_full")
    fixture_rank = [d for d in order if d in set(fixture_docs.values())]
    assert pos[b] < 10 and fixture_rank.index(b) < 5, f"B is at {pos[b] + 1} overall, {fixture_rank.index(b) + 1} among fixture people"
    # real Java developers from the CSVs cover all four terms too, so the full matches only make the top 15
    assert all(pos[x] < 15 for x in full), [pos[x] + 1 for x in full]
    assert e in pos and pos[e] > pos[b] and pos[e] > 30, "E (Java listed only) is in, far below B"


def test_like_only_ranks_when_terms_are_explicit(ctx):
    plain = verbs.search(ctx, Criteria(skills=["elixir"]))
    ranked = verbs.search(ctx, Criteria(skills=["elixir"], like=str(JD)))
    s = ctx.session()
    assert set(s.get(plain.data["set"]).order) == set(s.get(ranked.data["set"]).order) == set(s.get(plain.data["set"]).order)
    assert "ranked by like java-backend.md" in ranked.text.splitlines()[0]


def test_like_by_document_id_and_bare_file_name(ctx, fixture_docs):
    full = _specials(fixture_docs, "java_full")[0]
    doc_id = ctx.index.ids([full])[full]
    out = verbs.search(ctx, Criteria(like=doc_id))
    assert ctx.session().get(out.data["set"]).order[0] == full, "a resume is most like itself"
    (ctx.session().dir / "jd.md").write_text(JD.read_text())
    out2 = verbs.search(ctx, Criteria(like="jd.md"))
    assert out2.data["like"]["source"] == "jd.md" and out2.data["count"] == out.data["count"] or out2.data["count"] > 0
    with pytest.raises(ResumesError) as err:
        verbs.search(ctx, Criteria(like="no-such-file.md"))
    assert err.value.code == "LIKE_SOURCE_NOT_FOUND"


def test_like_with_constraints_reports_unknown_years(ctx):
    out = verbs.search(ctx, Criteria(like=str(JD), min_years=5))
    assert "min-years=5" in out.text.splitlines()[0]
    assert out.data["unknown_years"] >= 1 and "with unknown years" in out.text.splitlines()[0]
