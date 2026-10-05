"""Set algebra, the server-side judge and vocabulary edits."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agentic_search import config as config_mod
from agentic_search.errors import ResumesError
from agentic_search.index.store import current_path
from agentic_search.query import verbs
from agentic_search.query.search import Criteria
from agentic_search.query.verbs import Context
from agentic_search.vocab import review as vr
from tests.testroot import TEST_ROOT

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
needs_index = pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built")


@pytest.fixture
def ctx(tmp_path):
    cfg = config_mod.load(TEST_ROOT)
    cfg.query.sessions = tmp_path / "sessions"
    return Context(cfg, session_id="t")


@needs_index
def test_union_and_minus(ctx):
    a = verbs.search(ctx, Criteria(skills=["elixir"]))
    b = verbs.search(ctx, Criteria(topics=["data pipelines"]))
    s = ctx.session()
    oa, ob = s.get(a.data["set"]).order, s.get(b.data["set"]).order
    u = verbs.union(ctx, "rs_01", "rs_02")
    m = verbs.minus(ctx, "rs_02", "rs_01")
    ou, om = s.get(u.data["set"]).order, s.get(m.data["set"]).order
    assert ou == oa + [d for d in ob if d not in set(oa)] and len(set(ou)) == len(ou)
    assert om == [d for d in ob if d not in set(oa)] and len(om) == len(ob) - 25
    assert u.text.startswith("rs_03 · ") and "rs_01 ∪ rs_02" in u.text and "rs_02 − rs_01" in m.text
    assert s.get("rs_04").parent == "rs_02"


@needs_index
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="judge calls OpenAI")
def test_server_side_judge_records_a_judgment(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    flt = verbs.filter_(ctx, "rs_01", Criteria(skills=["elixir"]))
    out = verbs.judge(ctx, flt.data["set"], criterion="talented; decent price = rate ≤ p50", batch=25)
    assert out.data["judgment"] == "j_01" and out.data["n_scored"] >= 0.9 * flt.data["count"]
    assert out.data["usage"]["calls"] >= 1 and "via resumes judge" in (ctx.session().scores_dir / "j_01.json").read_text()
    srt = verbs.sort_(ctx, flt.data["set"], "judgment,rate")
    assert "★" in srt.text


def test_vocab_edits_on_a_copy(tmp_path):
    path = tmp_path / "vocab.csv"
    shutil.copy(ROOT / "schema" / "vocab.csv", path)
    n = len(vr.load_rows(path))
    row = vr.add(path, kind="skill", slug="zig", canonical="Zig", aliases=["Zig lang"], implies=[])
    assert row["slug"] == "zig" and len(vr.load_rows(path)) == n + 1
    with pytest.raises(ResumesError):
        vr.add(path, kind="skill", slug="zig2", canonical="Elixir")          # a form owned by another term
    with pytest.raises(ResumesError):
        vr.add(path, kind="skill", slug="Bad Slug", canonical="x")
    assert "ziglang" in vr.alias(path, "zig", "ziglang")["aliases"]
    merged = vr.merge(path, "zig", "elixir")
    assert "Zig" in merged["aliases"] and all(r["slug"] != "zig" for r in vr.load_rows(path))
    vr.lint(vr.load_rows(path))
    unresolved = tmp_path / "unresolved_terms.json"
    unresolved.write_text('[["Leadership", 186], ["Elixir/OTP", 3], ["Kubernetes admin", 2]]')
    rows = vr.review(path, unresolved, top=3)
    assert rows[1]["already"] == "elixir" and rows[2]["nearest"] is not None and rows[0]["count"] == 186
