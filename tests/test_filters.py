"""Browse at any size, rank only small sets, add and remove filters (skipped when the index is not built)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from pyroaring import BitMap

from agentic_search import config as config_mod
from agentic_search.errors import ResumesError
from agentic_search.index.store import current_path
from agentic_search.query import search as search_mod
from agentic_search.query import text as text_mod
from agentic_search.query import verbs, view
from agentic_search.query.index import Index
from agentic_search.query.search import Criteria
from agentic_search.query.verbs import Context
from agentic_search.session import filters as fmod
from agentic_search.session import judgments as jmod
from tests.testroot import TEST_ROOT

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
pytestmark = pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built")
TEXT_QUERY = "nightly batch jobs that moved data between systems"      # the query `resumes bench` caches


@pytest.fixture
def ctx(tmp_path):
    cfg = config_mod.load(TEST_ROOT)
    cfg.query.sessions = tmp_path / "sessions"
    return Context(cfg, session_id="t")


def order_of(ctx, out) -> list[int]:
    return ctx.session().get(out.data["set"]).order


def write_scores(ctx, docs, *, criterion="talented; decent price = rate ≤ p50", judgment=None, score=lambda d: (d * 7) % 101):
    ids = ctx.index.ids(docs)
    payload = {"criterion": criterion, "judge": "pytest", "scores": [{"id": ids[d], "score": score(d), "note": f"n{d}"} for d in docs]}
    if judgment:
        payload["judgment"] = judgment
    ctx.session().scores_path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------- add and remove filters


def test_remove_a_filter_brings_the_previous_results_back(ctx):
    first = verbs.search(ctx, Criteria(topics=["data pipelines"]))
    flt = verbs.filter_(ctx, None, Criteria(skills=["elixir"]), said="only those who know elixir")
    assert flt.data["count"] == 25 and flt.text.splitlines()[-1].endswith("f1 topic=data-pipelines · f2 skill=elixir")
    back = verbs.drop(ctx, ["elixir"])
    assert back.data["count"] == first.data["count"] == 165
    assert order_of(ctx, back) == order_of(ctx, first), "the same people in the same order"
    assert back.data["reused"] == "rs_01", "a combination seen before reuses its saved order"
    assert back.text.startswith("rs_03 · 165 people · removed f2 skill=elixir")
    assert back.text.splitlines()[-1] == "— rs_03 · 165 · page 0/17 · f1 topic=data-pipelines"
    # by id, and the first filter instead of the last one
    verbs.filter_(ctx, None, Criteria(skills=["elixir"]))
    only_elixir = verbs.drop(ctx, ["f1"])
    alone = verbs.search(ctx, Criteria(skills=["elixir"]))
    assert only_elixir.data["count"] == alone.data["count"] == 56
    assert order_of(ctx, only_elixir) == order_of(ctx, alone), "as if elixir had been the question from the start"
    assert ctx.session().get(only_elixir.data["set"]).parent == "rs_04", "a removal is a step in the history: `back` undoes it"


def test_members_do_not_depend_on_the_order_of_adding(ctx):
    conditions = [Criteria(topics=["data pipelines"]), Criteria(skills=["elixir"]), Criteria(rate_max=80), Criteria(remote=True)]
    seen = set()
    for perm in itertools.permutations(range(4)):
        verbs.search(ctx, Criteria(**vars(conditions[perm[0]])))
        for i in perm[1:]:
            out = verbs.filter_(ctx, None, Criteria(**vars(conditions[i])), show=False)
        seen.add(frozenset(order_of(ctx, out)))
    assert len(seen) == 1 and 0 < len(next(iter(seen))) < 25
    assert len(ctx.session().filter_ids()) == 4, "24 orders of adding, four saved filters"


def test_adding_equals_computing_from_scratch(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    out = verbs.filter_(ctx, None, Criteria(rate_max=80, remote=True), show=False)
    rs = ctx.session().get(out.data["set"])
    assert rs.filters == ["f1", "f2", "f3", "f4"], "one call, two constraints: each its own filter"
    # from scratch: a second index handle, all conditions evaluated in one go
    ix = Index(ctx.cfg)
    whole = search_mod.evaluate(ix, ctx.cfg, Criteria(topics=["data pipelines"], skills=["elixir"], rate_max=80, remote=True))
    assert set(rs.order) == set(whole.order)
    chain = search_mod.evaluate(ix, ctx.cfg, Criteria(topics=["data pipelines"]))
    assert rs.order == [d for d in chain.order if d in set(rs.order)], "order: the first filter's ranking, restricted"
    # and the saved order equals a fresh materialisation with no saved views
    ctx.session().views_path.unlink()
    fresh, reused, _ = view.materialize(ctx.index, ctx.session(), view.filters_of(ctx.index, ctx.cfg, ctx.session(), rs), rs.sort)
    assert reused is None and fresh == rs.order


def test_constraints_of_one_call_can_be_removed_one_by_one(ctx):
    out = verbs.search(ctx, Criteria(skills=["java"], min_years=5))
    head = out.text.splitlines()[0]
    assert head.startswith("rs_01 · 98 people · skill java (incl.: spring") and head.endswith("· min-years=5 · +18 with unknown years (--include-unknown)")
    assert out.text.splitlines()[-1].endswith("f1 skill=java · f2 min-years=5")
    wider = verbs.drop(ctx, ["years"], show=False)
    assert wider.data["count"] == verbs.search(ctx, Criteria(skills=["java"])).data["count"]


def test_the_same_condition_is_the_same_filter(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    a = verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    verbs.drop(ctx, ["f2"], show=False)
    before = ctx.session().filter_path("f2").stat().st_mtime_ns
    b = verbs.filter_(ctx, None, Criteria(skills=["Elixir "]), show=False)
    assert ctx.session().get(b.data["set"]).filters == ["f1", "f2"] and b.data["reused"] == a.data["set"]
    assert ctx.session().filter_path("f2").stat().st_mtime_ns == before, "not computed again, not written again"
    assert ctx.session().filter_ids() == ["f1", "f2"]


def test_removing_never_searches(ctx, monkeypatch):
    """drop / clear / filters / paging work from the saved files alone."""
    have_text = bool(list((CFG.index.cache / "queries").glob("*.json"))) if (CFG.index.cache / "queries").is_dir() else False
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    verbs.filter_(ctx, None, Criteria(rate_max=90), show=False)
    if have_text:
        try:
            verbs.filter_(ctx, None, Criteria(text=TEXT_QUERY), show=False)
        except ResumesError as e:                       # no key and the query is not in the cache
            assert e.code == "EMBEDDER_UNAVAILABLE"
            have_text = False

    def boom(*a, **k):
        raise AssertionError("a removal searched the index")

    monkeypatch.setattr(Index, "bitmaps", boom)
    monkeypatch.setattr(Index, "member_scores", boom)
    monkeypatch.setattr(Index, "load_fts", boom)
    monkeypatch.setattr(search_mod, "evaluate", boom)
    monkeypatch.setattr(view, "evaluate", boom)
    monkeypatch.setattr(search_mod, "search_text", boom)
    monkeypatch.setattr(search_mod, "analyse", boom)
    monkeypatch.setattr(text_mod.QueryEmbedder, "_compute", boom)
    fresh = Context(ctx.cfg, session_id="t")            # a new process: nothing cached in memory
    n = len(fresh.session().get(fresh.session().meta["current"]).filters)
    assert n == (4 if have_text else 3)
    assert verbs.filters_(fresh).data["filters"][1]["without"] >= 25
    assert verbs.drop(fresh, ["elixir"]).data["count"] > 0
    assert verbs.drop(fresh, ["f3"], show=True).data["count"] > 0
    if have_text:
        assert verbs.drop(fresh, ["f1"], show=False).data["count"] > 0, "the text filter alone: still no scan"
    assert verbs.next_(fresh).data["cards"]
    assert verbs.clear(fresh, show=True).data["count"] == 3026
    assert verbs.page(fresh, 10).data["start"] == 91
    assert verbs.sort_(fresh, None, "rate").data["cards"]
    with pytest.raises(AssertionError):                  # the guard itself works: a new condition does search
        verbs.filter_(fresh, None, Criteria(skills=["python"]))


def test_drop_says_what_it_could_not_find(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    verbs.filter_(ctx, None, Criteria(rate_max=80, min_years=3), show=False)
    for target, code in (("python", "UNKNOWN_FILTER"), ("f9", "UNKNOWN_FILTER"), ("rank", "NO_RANKING")):
        with pytest.raises(ResumesError) as e:
            verbs.drop(ctx, [target])
        assert e.value.code == code, target
    assert "active: f1 topic=data-pipelines · f2 skill=elixir · f3 min-years=3 · f4 rate-max=80" in str(e.value) or code == "NO_RANKING"
    with pytest.raises(ResumesError) as e:
        verbs.drop(ctx, ["f9"])
    assert "active: f1 topic=data-pipelines · f2 skill=elixir · f3 min-years=3 · f4 rate-max=80" in e.value.message
    ctx.session().get("rs_03")
    f3 = fmod.load(ctx.session(), "f3")
    f3.said = "at least three years, elixir people"
    fmod.save(ctx.session(), f3)
    with pytest.raises(ResumesError) as e:
        verbs.drop(ctx, ["elixir"])
    assert e.value.code == "AMBIGUOUS_FILTER" and "f2, f3" in e.value.message
    both = verbs.drop(ctx, ["f2", "rate"], show=False)
    assert ctx.session().get(both.data["set"]).filters == ["f1", "f3"]
    with pytest.raises(ResumesError) as e:
        verbs.drop(ctx, [])
    assert e.value.code == "NO_TARGET"


def test_clear_and_filters_listing(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]), said="who worked on data pipelines?")
    verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    listing = verbs.filters_(ctx)
    assert listing.text.splitlines()[0] == 'f1  topic=data-pipelines · 165 alone · without it: 56 · "who worked on data pipelines?"'
    assert listing.text.splitlines()[1] == "f2  skill=elixir · 56 alone · without it: 165"
    assert [f["id"] for f in listing.data["filters"]] == ["f1", "f2"] and listing.data["ranking"] is None
    out = verbs.clear(ctx)
    rs = ctx.session().get(out.data["set"])
    assert rs.count == 3026 and rs.filters == [] and rs.order[:3] == sorted(rs.order, reverse=True)[:3], "all CVs, newest first"
    assert "no filters: all CVs" in verbs.filters_(ctx).text
    assert verbs.back(ctx).data["set"] == "rs_02", "clear is a step in the history too"


# ---------------------------------------------------------------- browse at any size


def test_browse_before_any_search_and_at_any_size(ctx):
    p10 = verbs.page(ctx, 10)
    assert p10.data["start"] == 91 and p10.data["end"] == 100 and p10.data["count"] == 3026
    assert p10.text.splitlines()[-1] == "— rs_01 · 3026 · page 10/303 · no filters (all CVs)"
    docs = [c["doc_no"] for c in p10.data["cards"]]
    assert docs == sorted(docs, reverse=True), "newest first"
    assert verbs.next_(ctx).data["start"] == 101 and verbs.prev_(ctx).data["start"] == 91
    assert verbs.top(ctx, 25).data["end"] == 25 and verbs.page(ctx, 303).data["end"] == 3026
    assert ctx.session().set_ids() == ["rs_01"], "paging makes no new sets"
    # sorting is mechanical too: no limit
    by_rate = verbs.sort_(ctx, None, "rate")
    rates = [c["rate"] for c in by_rate.data["cards"]]
    assert by_rate.data["count"] == 3026 and rates == sorted(rates)
    # a first `filter` needs no `search` before it
    fresh = Context(ctx.cfg, session_id="t2")
    assert verbs.filter_(fresh, None, Criteria(skills=["elixir"])).data["count"] == 56
    assert "no sets yet" in verbs.sets(Context(ctx.cfg, session_id="t3")).text


# ---------------------------------------------------------------- rank only small sets; scores stick


def test_scores_stick_to_the_person_across_filter_changes(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    with pytest.raises(ResumesError) as e:
        verbs.cards(ctx, None)
    assert e.value.code == "TOO_MANY_TO_RANK"
    rate_limit = next(s["value"] for s in e.value.data["suggestions"] if s["flag"] == "--rate-max")
    elixir = verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    c1 = verbs.cards(ctx, None)
    first = [c["doc_no"] for c in c1.data["cards"]]
    assert len(first) == 25 and c1.text.startswith("rs_02 · 25 cards · rate p50 €") and not c1.data["anchors"]
    write_scores(ctx, first)
    assert verbs.score(ctx, None, source="scores.json").text.startswith("j_01 · rs_02 · 25 scored")
    ranked = verbs.sort_(ctx, None, "judgment,rate")
    assert ranked.text.splitlines()[-1].endswith("sorted judgment↓ rate↑ (j_01 25/25)")
    stars = {c["doc_no"]: (c["score"], c["note"]) for c in verbs.top(ctx, 25).data["cards"]}
    assert len(stars) == 25 and all(s is not None for s, _ in stars.values())

    # remove the filter: 165 again, the 25 judged on top with their stars; the people not judged yet follow,
    # by the rest of the sort (rate ↑), ties in the order of the first filter
    wide = verbs.drop(ctx, ["elixir"])
    rs = ctx.session().get(wide.data["set"])
    assert rs.count == 165 and set(rs.order) == set(ctx.session().get("rs_01").order)
    assert rs.order[:25] == ctx.session().get(ranked.data["set"]).order
    rates = {d: p.rate for d, p in ctx.index.profiles(rs.order).items()}
    pos = {d: i for i, d in enumerate(ctx.session().get("rs_01").order)}
    rest = [d for d in ctx.session().get("rs_01").order if d not in stars]
    assert rs.order[25:] == sorted(rest, key=lambda d: (rates[d] is None, rates[d] or 0, pos[d]))
    assert "★" not in "".join(verbs.page(ctx, 4).text.split("links:")[1]), "people the ranking does not cover carry no star"
    verbs.page(ctx, 1)
    assert wide.text.splitlines()[-1].endswith("f1 topic=data-pipelines · sorted judgment↓ rate↑ (j_01 25/165)")
    assert all("★" in ln for ln in wide.text.split("links:")[1].splitlines()[1:11])
    with pytest.raises(ResumesError):
        verbs.cards(ctx, None)                            # 165 again: too many to rank

    # another narrow view, partly judged: only the people not judged yet are printed, with anchors
    narrow = verbs.filter_(ctx, None, Criteria(rate_max=rate_limit), show=False)
    n_rs = ctx.session().get(narrow.data["set"])
    known = [d for d in n_rs.order if d in stars]
    assert n_rs.count <= 50 and 0 < len(known) < n_rs.count
    c2 = verbs.cards(ctx, None)
    second = [c["doc_no"] for c in c2.data["cards"]]
    assert set(second) == set(n_rs.order) - set(known) and not (set(second) & set(first)), "nobody is judged twice"
    assert f"({len(known)} of {n_rs.count} already judged)" in c2.text.splitlines()[0]
    assert 'same criterion as j_01 "talented; decent price = rate ≤ p50"' in c2.text and '"judgment":"j_01"' in c2.text.splitlines()[0]
    assert len(c2.data["anchors"]) == 6 and "anchors (judged before under j_01" in c2.text
    anchor_ids = {a["doc_no"] for a in c2.data["anchors"]}
    by_score = sorted(stars, key=lambda d: (-stars[d][0], d))
    assert anchor_ids == set(by_score[:3]) | set(by_score[-3:])

    # the agent echoes an anchor with another score: its score stands
    write_scores(ctx, second + [by_score[0]], judgment="j_01", score=lambda d: 1)
    merged = verbs.score(ctx, None, source="scores.json")
    assert merged.data["judgment"] == "j_01" and merged.data["merged"] and merged.data["n_scored"] == len(second)
    assert merged.data["n_kept"] == 1 and merged.data["judged"] == n_rs.count
    assert (ctx.session().scores_dir / f"j_01+{n_rs.id}.json").is_file()
    assert [j["id"] for j in jmod.list_judgments(ctx.session())] == ["j_01", "j_01"], "one judgment, two passes"
    assert "all judged under j_01" in verbs.cards(ctx, None).text
    page = verbs.top(ctx, 50)
    for c in page.data["cards"]:
        assert (c["score"], c["note"]) == (stars[c["doc_no"]] if c["doc_no"] in stars else (1, f"n{c['doc_no']}"))

    # add elixir back: the stars of the first pass, unchanged
    again = verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    verbs.drop(ctx, ["rate"], show=False)
    final = verbs.top(ctx, 25)
    assert final.data["count"] == 25 and {c["doc_no"]: (c["score"], c["note"]) for c in final.data["cards"]} == stars
    assert again.data["count"] == len(known)


def test_scores_without_the_judgment_field(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    docs = ctx.session().get("rs_02").order
    write_scores(ctx, docs[:20])
    with pytest.raises(ResumesError) as e:
        verbs.score(ctx, None, source="scores.json")
    assert e.value.code == "SCORES_INCOMPLETE"
    assert verbs.score(ctx, None, source="scores.json", allow_partial=True).data["n_missing"] == 5
    # the same criterion, word for word, is the same judgment even when the file does not name it
    write_scores(ctx, docs[20:], criterion="Talented;  decent price = rate ≤ p50 ")
    out = verbs.score(ctx, None, source="scores.json")
    assert out.data["judgment"] == "j_01" and out.data["merged"] and out.data["judged"] == 25
    # another criterion is another judgment, and it has to cover the set
    write_scores(ctx, docs[:3], criterion="leadership")
    with pytest.raises(ResumesError) as e:
        verbs.score(ctx, None, source="scores.json")
    assert e.value.code == "SCORES_INCOMPLETE" and 'put "judgment":"j_01" in the file' in e.value.message
    write_scores(ctx, docs, criterion="leadership")
    assert verbs.score(ctx, None, source="scores.json").data["judgment"] == "j_02"
    with pytest.raises(ResumesError) as e:
        verbs.score(ctx, None, source="scores.json", into="j_07")
    assert e.value.code == "UNKNOWN_JUDGMENT"


def test_remove_the_ranking(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    base = ctx.session().get("rs_02").order
    write_scores(ctx, base)
    verbs.score(ctx, None, source="scores.json")
    verbs.sort_(ctx, None, "judgment,rate")
    assert 'rank  j_01 "talented; decent price = rate ≤ p50" · 25 of 25 judged · sorted judgment↓ rate↑' in verbs.filters_(ctx).text
    plain = verbs.drop(ctx, ["rank"])
    rs = ctx.session().get(plain.data["set"])
    assert rs.order == base and rs.sort == [] and "★" not in plain.text and "j_01" not in plain.text.splitlines()[-1]
    assert "★" not in verbs.filter_(ctx, None, Criteria(remote=True)).text, "stays off for the sets made from it"
    back = verbs.sort_(ctx, None, "judgment")
    assert "★" in back.text, "`sort --by judgment` brings the ranking back; the scores were never lost"


# ---------------------------------------------------------------- sets that are not an intersection of conditions


def test_old_sets_and_set_algebra_become_one_frozen_filter(ctx):
    verbs.search(ctx, Criteria(skills=["elixir"]))
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    s = ctx.session()
    # a set file in the older format, without filters
    d = json.loads(s.set_path("rs_02").read_text())
    d.pop("filters"), d.pop("ranking_off")
    s.set_path("rs_02").write_text(json.dumps(d))
    assert verbs.use(ctx, "rs_02").text.splitlines()[-1] == "— rs_02 · 165 · page 0/17 · from search (topic=data-pipelines)"
    sub = verbs.filter_(ctx, "rs_02", Criteria(skills=["elixir"]), show=False)
    rs = s.get(sub.data["set"])
    assert rs.count == 25 and rs.order == [x for x in s.get("rs_02").order if x in set(rs.order)]
    assert sub.text.splitlines()[-1].endswith("f3 rs_02 · f1 skill=elixir")
    assert verbs.drop(ctx, ["f1"], show=False).data["count"] == 165
    u = verbs.union(ctx, "rs_01", "rs_02")
    ur = s.get(u.data["set"])
    assert ur.count == 56 + 165 - 25 and len(ur.filters) == 1 and u.text.splitlines()[-1].endswith(f"{ur.filters[0]} rs_01 ∪ rs_02")
    cheap = verbs.filter_(ctx, None, Criteria(rate_max=60), show=False)
    assert 0 < cheap.data["count"] < ur.count
    assert s.get(cheap.data["set"]).order == [x for x in ur.order if x in set(s.get(cheap.data["set"]).order)]
    assert verbs.drop(ctx, ["rate"], show=False).data["count"] == ur.count


def test_a_filter_saved_against_another_index_version_is_evaluated_again(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    verbs.filter_(ctx, None, Criteria(skills=["elixir"]), show=False)
    s = ctx.session()
    f = fmod.load(s, "f2")
    f.index_version, f.members, f.order = "older", BitMap([1, 2, 3]), [3, 2, 1]
    fmod.save(s, f)
    out = verbs.drop(ctx, ["f1"], show=False)
    assert out.data["count"] == 56 and fmod.load(s, "f2").index_version == ctx.index.version


# ---------------------------------------------------------------- every facet the agent may propose is a filter


def test_availability_is_a_filter(ctx):
    out = verbs.search(ctx, Criteria(topics=["data pipelines"]))
    facet = dict(out.data["facets"]["availability"])
    now = verbs.filter_(ctx, "rs_01", Criteria(availability=["now"]), show=False)
    assert now.data["count"] == facet["now"] == 31 and now.text.splitlines()[-1].endswith("f1 topic=data-pipelines · f2 availability=now")
    soon = verbs.filter_(ctx, "rs_01", Criteria(availability=["2 weeks", "immediately"]), show=False)
    assert soon.data["count"] == facet["now"] + facet["2w"], "a list is any of them; the words are read like the cards read them"
    same = verbs.filter_(ctx, "rs_01", Criteria(availability=["now", "2w"]), show=False)
    assert ctx.session().get(same.data["set"]).filters == ctx.session().get(soon.data["set"]).filters, "the same condition is the same filter"
    members = ctx.session().get(now.data["set"]).order
    assert all(p.availability and p.availability.lower().startswith("immediate") for p in ctx.index.profiles(members).values())
    assert verbs.drop(ctx, ["availability"], show=False).data["count"] == 165
    everyone = dict(verbs.search(ctx, Criteria()).data["facets"]["availability"])
    alone = verbs.search(ctx, Criteria(availability=["now"]))
    assert alone.data["count"] == everyone["now"] == 97, "people, not documents: 101 CVs say so, four of them older versions"
    assert alone.text.startswith(f"{alone.data['set']} · 97 people · availability=now")
    with pytest.raises(ResumesError) as e:
        verbs.search(ctx, Criteria(topics=["data pipelines"]))
        verbs.cards(ctx, None)
    assert {"flag": "--availability", "value": "now", "count": 31} == {k: v for k, v in next(s for s in e.value.data["suggestions"] if s["group"] == "avail").items() if k in ("flag", "value", "count")}
