"""Pure parts of the query layer: combination, ranking, sorting, term resolution, rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from pyroaring import BitMap

from agentic_search.errors import ResumesError
from agentic_search.query import render
from agentic_search.query.index import Profile
from agentic_search.query.search import _ranks, combine, describe_args, rrf
from agentic_search.query.sort import parse_keys, sort_order
from agentic_search.query.terms import resolve_one, resolve_phrases, suggestion
from agentic_search.query.text import QueryEmbedder
from agentic_search.session.store import ResultSet
from agentic_search.vocab.resolve import Term, Vocabulary


def test_combine_modes():
    a, b, c = BitMap([1, 2, 3]), BitMap([2, 3, 4]), BitMap([3, 4, 5])
    assert combine([a, b, c], "all") == BitMap([3])
    assert combine([a, b, c], "any") == BitMap([1, 2, 3, 4, 5])
    assert combine([a, b, c], 2) == BitMap([2, 3, 4])
    assert combine([], "all") == BitMap()


def test_ranks_share_ties_and_rrf_fuses():
    scores = {1: 3.0, 2: 3.0, 3: 1.0}
    assert _ranks(scores, [1, 2, 3, 4]) == {1: 1, 2: 1, 3: 3, 4: 4}
    fused = rrf([1, 2, 3], [{1: 1.0, 2: 0.5, 3: 0.1}, {3: 9.0, 1: 5.0, 2: 1.0}])
    assert fused[1] > fused[3] > fused[2]
    assert rrf([1, 2], [{}, {1: 1.0}]) == rrf([1, 2], [{1: 1.0}]), "empty lists are ignored"


def test_describe_args():
    assert describe_args({"topic": ["data pipelines"], "skill": ["elixir"], "min_years": 5, "remote": True}) == "topic=data-pipelines skill=elixir min-years=5 --remote"
    assert describe_args({"mode": "any", "seniority": ["senior", "lead"], "rate_max": 80.0}) == "--any rate-max=80 seniority=senior,lead"
    assert describe_args({"by": ["judgment", "rate"]}) == "judgment,rate"


def _profile(doc_no, rate=None, years=None, seniority=None):
    return Profile(doc_no, f"r{doc_no:06d}", "T", seniority, years, rate, "EUR", "document", None, None, None, f"corpus/md/r{doc_no:06d}.md", False, None)


class _Index:
    def __init__(self, profiles):
        self._p = {p.doc_no: p for p in profiles}

    def profiles(self, docs):
        return {d: self._p[d] for d in docs if d in self._p}


def test_sort_keys_and_missing_last():
    assert parse_keys("judgment,rate") == [("judgment", "desc"), ("rate", "asc")]
    assert parse_keys("years:asc") == [("years", "asc")]
    for bad in ("nope", "rate:sideways", ""):
        with pytest.raises(ResumesError):
            parse_keys(bad)
    ix = _Index([_profile(1, rate=90, years=3, seniority="senior"), _profile(2, rate=50, years=None, seniority="junior"),
                 _profile(3, rate=None, years=10, seniority="lead"), _profile(4, rate=50, years=7, seniority=None)])
    order = [1, 2, 3, 4]
    assert sort_order(ix, order, parse_keys("rate"), scores=None, relevance=order) == [2, 4, 1, 3]
    assert sort_order(ix, order, parse_keys("years"), scores=None, relevance=order) == [3, 4, 1, 2]
    assert sort_order(ix, order, parse_keys("seniority"), scores=None, relevance=order) == [3, 1, 2, 4]
    scores = {1: (50, ""), 2: (90, ""), 3: (None, ""), 4: (90, "")}
    assert sort_order(ix, order, parse_keys("judgment,rate"), scores=scores, relevance=order) == [2, 4, 1, 3], "score desc, rate asc, null last"
    assert sort_order(ix, [3, 1, 2], parse_keys("relevance"), scores=None, relevance=[1, 2, 3]) == [1, 2, 3]


def _vocab():
    return Vocabulary([
        Term(1, "skill", "elixir", "Elixir", ["Elixir/OTP"], [], set(), set(), ""),
        Term(2, "skill", "phoenix-framework", "Phoenix Framework", ["Phoenix", "LiveView"], ["elixir"], {"phoenix"}, set(), ""),
        Term(3, "skill", "etl", "ETL", ["ELT"], ["data-pipelines"], set(), set(), ""),
        Term(4, "topic", "data-pipelines", "data pipelines", ["data pipeline", "CDC"], [], {"cdc"}, set(), ""),
        Term(5, "skill", "kubernetes", "Kubernetes", ["k8s"], [], set(), set(), ""),
    ])


def test_term_resolution():
    v = _vocab()
    assert [r.slug for r in resolve_one(v, "data pipelines", "topic")] == ["data-pipelines"]
    assert [r.slug for r in resolve_one(v, "Data-Pipelines", "topic")] == ["data-pipelines"]
    assert [r.slug for r in resolve_one(v, "k8s", "skill")] == ["kubernetes"]
    r = resolve_one(v, "ETL stuff", "topic")[0]
    assert (r.slug, r.kind, r.how) == ("data-pipelines", "topic", "via-skill:etl") and r.echo == '"ETL stuff" → data-pipelines (via etl)'
    assert resolve_one(v, "ETL stuff", "skill")[0].slug == "etl", "asked for as a skill, the skill stays"
    assert [r.slug for r in resolve_one(v, "elixir and kubernetes people", "skill")] == ["elixir", "kubernetes"]
    assert resolve_one(v, "moved data nightly", "topic") == []
    assert suggestion(v, "elixer")[0] == "elixir" and suggestion(v, "moved data nightly between systems") is None
    with pytest.raises(ResumesError) as e:
        resolve_phrases(v, ["elixer"], "skill")
    assert e.value.code == "UNRESOLVED_TERM" and "did you mean elixir" in e.value.message
    resolved, unresolved = resolve_phrases(v, ["data pipelines", "moved data nightly", "CDC"], "topic")
    assert [r.slug for r in resolved] == ["data-pipelines"] and unresolved == ["moved data nightly"]


def test_query_embedder_caches_to_json(tmp_path, monkeypatch):
    qe = QueryEmbedder("openai:text-embedding-3-large", 4, tmp_path)
    calls = []
    monkeypatch.setattr(qe, "_openai_many", lambda texts: [calls.append(t) or [0.1, 0.2, 0.3, 0.4] for t in texts])
    assert qe.embed("hello") == [0.1, 0.2, 0.3, 0.4]
    assert qe.embed("hello") == [0.1, 0.2, 0.3, 0.4] and calls == ["hello"], "second call is served from the cache"
    files = list((tmp_path / "queries").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["text"] == "hello"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ResumesError) as e:
        QueryEmbedder("openai:text-embedding-3-large", 4, None).embed("x")
    assert e.value.code == "EMBEDDER_UNAVAILABLE"


def test_render_pieces():
    assert render.fmt_years(9.2) == "9y" and render.fmt_years(1.5) == "1.5y" and render.fmt_years(None) == "?y"
    assert render.fmt_rate(72, "EUR") == "€72/h" and render.fmt_rate(None, "EUR") == "rate ?"
    card = "r000412 · Senior Data Engineer · 9y · €68/h\nskills: Python\n→ file:///x/r000412.md"
    assert render.card_text(card, (86, "streaming depth"), 1).split("\n")[0] == "r000412 · Senior Data Engineer · 9y · €68/h (+1 other version) ★86 streaming depth"
    stored = "r000412 · Senior Data Engineer · 9y · €68/h\nskills: Python"
    assert render.card_text(card, None) == stored, "a link stored by an older index is dropped"
    assert render.card_text(stored, None, link="file:///y/r000412.md") == stored + "\n→ file:///y/r000412.md"
    p = _profile(412, rate=68, years=9.2)
    p.title = "SENIOR DATA ENGINEER"
    line = render.link_line(11, p, "file:///x/r000412.md", (86, "streaming depth; rate under p50"), tty=False)
    assert line == "11. Senior Data Engineer · 9y · €68/h · ★86 streaming depth; rate under p50 · file:///x/r000412.md"
    assert "\x1b]8;;file:///x/r000412.md\x1b\\" in render.link_line(1, p, "file:///x/r000412.md", None, tty=True)
    rs = ResultSet("rs_03", "rs_02", "filter", {"skill": ["elixir"]}, "v1", 25, list(range(25)), "j_01", ["judgment:desc", "rate:asc"], 10, 10, "")
    assert render.state_line(rs) == "— rs_03 · 25 · page 1/3 · sorted judgment↓ rate↑ · from rs_02 (filter skill=elixir)"
    # a set made from saved filters names them (what `resumes drop` can remove) and how far the ranking covers it
    fl = [("f1", "topic=data-pipelines"), ("f2", "skill=elixir")]
    assert render.state_line(rs, filters=fl, judged=("j_01", 25)) == \
        "— rs_03 · 25 · page 1/3 · f1 topic=data-pipelines · f2 skill=elixir · sorted judgment↓ rate↑ (j_01 25/25)"
    plain = ResultSet("rs_03", "rs_02", "filter", {"skill": ["elixir"]}, "v1", 25, list(range(25)), "j_01", [], 10, 10, "")
    assert render.state_line(plain, filters=fl, judged=("j_01", 7)) == "— rs_03 · 25 · page 1/3 · f1 topic=data-pipelines · f2 skill=elixir · j_01 7/25 judged"
    assert render.state_line(plain, filters=[]) == "— rs_03 · 25 · page 1/3 · no filters (all CVs)"
    big = ResultSet("rs_01", None, "search", {}, "v1", 165, list(range(165)))
    assert "ranking needs ≤ 50 people" in render.next_hint(big, 50) and "resumes cards" not in render.next_hint(big, 50)
    assert "`resumes cards rs_03` to rank" in render.next_hint(rs, 50)
    rs.cursor = 25
    assert render.state_line(rs, end=True, index_changed=True).endswith("page 3/3 · end of set · sorted judgment↓ rate↑ · from rs_02 (filter skill=elixir) · (index updated since this set was made)")
    root = ResultSet("rs_01", None, "search", {"topic": ["data pipelines"]}, "v1", 107, [], None, [], 0, 10, "")
    assert render.state_line(root) == "— rs_01 · 107 · page 0/11 · from search (topic=data-pipelines)"
    f = {"count": 107, "strong": 96, "weak": 11, "years": {"p25": 4, "p50": 7, "p75": 11, "unknown": 0},
         "rate": {"p25": 58, "p50": 72, "p75": 90, "synthetic": 6, "currency": "EUR"}, "seniority": [("junior", 9), ("senior", 48)],
         "skills": [("python", 88)], "where": {"remote_ok": 61, "top": [("Berlin", 14)]}, "availability": [("2w", 20)]}
    lines = render.facet_lines(f)
    assert lines[0] == "evidence  strong 96 · weak 11" and lines[2] == "years     p25 4 · p50 7 · p75 11"
    assert lines[3] == "rate €/h  p25 58 · p50 72 · p75 90   (synthetic rate for 6 of 107)"
    assert lines[5] == "where     remote-ok 61 · Berlin 14"
