"""The query verbs, called in-process against the built index (skipped when not built)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_search import config as config_mod
from agentic_search.errors import ResumesError
from agentic_search.index.store import current_path
from agentic_search.query import verbs
from agentic_search.query.search import Criteria
from agentic_search.query.verbs import Context
from tests.testroot import TEST_ROOT

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
pytestmark = pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built")
GT = json.loads((ROOT / "data" / "fixture" / "ground_truth.json").read_text(encoding="utf-8"))["docs"]


@pytest.fixture
def ctx(tmp_path):
    cfg = config_mod.load(TEST_ROOT)
    cfg.query.sessions = tmp_path / "sessions"
    return Context(cfg, session_id="t")


@pytest.fixture(scope="module")
def fixture_docs():
    cfg = config_mod.load(TEST_ROOT)
    ix = verbs.Index(cfg)
    return dict(ix.con.execute("SELECT source_id, doc_no FROM docs WHERE source = 'fixture'").fetchall())


def _special(fixture_docs, name):
    return next(fixture_docs[sid] for sid, g in GT.items() if g["special"] == name)


def test_search_prints_count_and_facets_not_cards(ctx):
    out = verbs.search(ctx, Criteria(topics=["data pipelines"]))
    assert out.text.startswith("rs_01 · ") and "people · topic data-pipelines (expanded:" in out.text
    assert "cards:" not in out.text and "file://" not in out.text
    assert "evidence  strong" in out.text and "rate €/h  p25" in out.text and "(synthetic rate for" in out.text
    assert out.text.rstrip().splitlines()[-1].startswith("— rs_01 · ") and "page 0/" in out.text
    assert out.data["facets"]["count"] == out.data["count"] and out.data["state"]["cursor"] == 0


def test_skill_search_equals_filter_of_all(ctx):
    a = verbs.search(ctx, Criteria(skills=["elixir"]))
    everything = verbs.search(ctx, Criteria())
    b = verbs.filter_(ctx, everything.data["set"], Criteria(skills=["elixir"]))
    s = ctx.session()
    assert set(s.get(a.data["set"]).order) == set(s.get(b.data["set"]).order)
    assert a.data["count"] == 56


def test_elixir_membership_matches_ground_truth(ctx, fixture_docs):
    out = verbs.search(ctx, Criteria(skills=["elixir"]))
    members = set(ctx.session().get(out.data["set"]).order)
    truth = {fixture_docs[s] for s, g in GT.items() if g["elixir"] != "none"}
    controls = {fixture_docs[s] for s, g in GT.items() if g["negative_control"]}
    phoenix_only = {fixture_docs[s] for s, g in GT.items() if g["elixir"] == "phoenix_only"}
    assert truth <= members and not (controls & members) and phoenix_only <= members


def test_phrase_resolution_and_echo(ctx):
    out = verbs.search(ctx, Criteria(topics=["ETL stuff"]))
    assert 'resolved  "ETL stuff" → data-pipelines (via etl)' in out.text
    out = verbs.search(ctx, Criteria(skills=["k8s"]))
    assert out.data["resolved"][0]["slug"] == "kubernetes" and 'resolved  "k8s" → kubernetes' in out.text
    with pytest.raises(ResumesError) as e:
        verbs.search(ctx, Criteria(skills=["elixer"]))
    assert e.value.code == "UNRESOLVED_TERM" and "did you mean elixir" in e.value.message


def test_near_duplicates_count_once(ctx):
    out = verbs.search(ctx, Criteria(topics=["data pipelines"]))
    order = ctx.session().get(out.data["set"]).order
    ix = ctx.index
    groups = [ix.dup_group_of[d] for d in order if d in ix.dup_group_of]
    assert len(groups) == len(set(groups)), "one version per person"
    assert groups, "the fixture's near-duplicate pipeline people are in the set"
    kept = [d for d in order if d in ix.dup_group_of]
    assert all(d == max(ix.dup_groups[ix.dup_group_of[d]]) for d in kept), "the newest version is the representative"
    page = verbs.top(ctx, len(order))
    assert "(+1 other version)" in page.text


def test_java_jd_loose_query_ranks_by_coverage(ctx, fixture_docs):
    out = verbs.search(ctx, Criteria(skills=["Java", "Spring", "Hibernate", "REST APIs"], mode="any"))
    assert " OR " in out.text.splitlines()[0]
    rs = ctx.session().get(out.data["set"])
    full = _special(fixture_docs, "java_full")
    partial = _special(fixture_docs, "java_partial")
    b = _special(fixture_docs, "B")
    pos = {d: i for i, d in enumerate(rs.order)}
    assert full in pos and partial in pos and pos[full] < pos[partial], "all four terms rank above two of four"
    assert b in pos, "B says only 'web services' and is still a member (rest-apis alias)"


def test_unknown_years_are_reported_not_hidden(ctx, fixture_docs):
    strict = verbs.search(ctx, Criteria(skills=["java"], min_years=5))
    assert strict.data["unknown_years"] >= 1 and "with unknown years (--include-unknown)" in strict.text.splitlines()[0]
    loose = verbs.search(ctx, Criteria(skills=["java"], min_years=5, include_unknown=True))
    assert loose.data["count"] == strict.data["count"] + strict.data["unknown_years"]
    unknown = set(ctx.session().get(loose.data["set"]).order) - set(ctx.session().get(strict.data["set"]).order)
    assert len(unknown) == strict.data["unknown_years"]
    assert all(p.years is None for p in ctx.index.profiles(list(unknown)).values()), "exactly the documents without years"
    # D hides every date; only the LLM may read its years from the prose
    d = _special(fixture_docs, "D")
    years, source = ctx.index.con.execute("SELECT years, years_source FROM profile WHERE doc_no = ?", [d]).fetchone()
    if years is None:
        assert d in unknown
    else:
        assert source == "llm" and d in set(ctx.session().get(loose.data["set"]).order) | set()


def test_e_java_is_listed_only_on_the_card(ctx, fixture_docs):
    e = _special(fixture_docs, "E")
    card = ctx.index.cards([e])[e]
    listed = card.split("listed only:")[1].split("\n")[0] if "listed only:" in card else ""
    assert "Java" in listed and "Java" not in card.split("listed only:")[0].split("skills:")[-1]


def test_strict_drops_weak_members(ctx):
    a = verbs.search(ctx, Criteria(topics=["data pipelines"]))
    b = verbs.search(ctx, Criteria(topics=["data pipelines"], strict=True))
    assert b.data["count"] == a.data["facets"]["strong"] and "evidence" not in b.text


def test_page_printing_rule(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    f1 = verbs.filter_(ctx, "rs_01", Criteria(seniority=["senior"]))
    assert "cards:" not in f1.text and "seniority senior" in f1.text, "parent not shown yet → facets"
    verbs.next_(ctx)                                   # show rs_02 page 1
    f2 = verbs.filter_(ctx, "rs_02", Criteria(rate_max=90))
    assert "cards:" in f2.text and "links:" in f2.text, "parent shown → page 1"
    assert ctx.session().get(f2.data["set"]).cursor == min(10, f2.data["count"])
    f3 = verbs.filter_(ctx, "rs_02", Criteria(rate_max=90), show=False)
    assert "cards:" not in f3.text, "--no-show overrides"
    s = verbs.search(ctx, Criteria(skills=["elixir"]), show=True)
    assert "cards:" in s.text and "evidence" not in s.text.split("cards:")[1]


def test_ranking_limit_refuses_and_suggests(ctx):
    """Above max_cards people `cards` refuses and names filters that really get under the limit."""
    limit = CFG.query.max_cards
    assert limit == 50
    out = verbs.search(ctx, Criteria(topics=["data pipelines"]))
    assert out.data["count"] > limit and "ranking needs ≤ 50 people" in out.text and "resumes cards" not in out.text
    with pytest.raises(ResumesError) as e:
        verbs.cards(ctx, "rs_01")
    assert e.value.code == "TOO_MANY_TO_RANK" and e.value.message.startswith(f"{out.data['count']} > {limit}")
    assert "narrow to ≤ 50 with:" in e.value.message and "`resumes next`" in e.value.message
    sugg = e.value.data["suggestions"]
    assert {s["group"] for s in sugg} >= {"skill", "seniority", "avail", "rate", "years"}
    for s in sugg:                                   # every promised count is the count the filter delivers
        assert 2 <= s["count"] <= limit
        crit = {"--skill": Criteria(skills=[str(s["value"])]), "--seniority": Criteria(seniority=[str(s["value"])]),
                "--location": Criteria(location=str(s["value"])), "--remote": Criteria(remote=True),
                "--rate-max": Criteria(rate_max=s["value"]), "--min-years": Criteria(min_years=s["value"]),
                "--availability": Criteria(availability=[str(s["value"])])}[s["flag"]]
        got = verbs.filter_(ctx, "rs_01", crit, show=False)
        assert got.data["count"] == s["count"], s
        cards = verbs.cards(ctx, got.data["set"])     # and that set can be ranked
        assert cards.data["shown"] == s["count"] and "→ file://" not in cards.text, "cards carry no link; pages and show do"
    assert "write scores to " + str(ctx.session().scores_path) in cards.text


def test_limit_is_exact(ctx):
    verbs.search(ctx, Criteria(topics=["data pipelines"]))
    order = ctx.session().get("rs_01").order
    s = ctx.session()
    from agentic_search.session import filters as fmod
    for n, ok in ((50, True), (51, False)):
        fz = fmod.frozen(s, label=f"first-{n}", order=order[:n], index_version=ctx.index.version)
        rs = s.new_set(parent="rs_01", op="test", args={}, index_version=ctx.index.version, order=order[:n], filters=[fz.id])
        if ok:
            assert verbs.cards(ctx, rs.id).data["shown"] == 50
        else:
            with pytest.raises(ResumesError) as e:
                verbs.cards(ctx, rs.id)
            assert e.value.code == "TOO_MANY_TO_RANK"


def test_score_sort_filter_inherit_judgment(ctx):
    out = verbs.search(ctx, Criteria(topics=["data pipelines"]))
    rs = ctx.session().get(out.data["set"])
    ids = ctx.index.ids(rs.order)
    payload = {"criterion": "test", "judge": "pytest", "scores": [{"id": ids[d], "score": (d * 7) % 101, "note": "n"} for d in rs.order]}
    ctx.session().scores_path.write_text(json.dumps(payload))
    sc = verbs.score(ctx, "rs_01", source="scores.json")
    assert sc.data["judgment"] == "j_01" and sc.data["n_scored"] == rs.count
    assert (ctx.session().scores_dir / "j_01.json").is_file()
    srt = verbs.sort_(ctx, "rs_01", "judgment,rate")
    order = ctx.session().get(srt.data["set"]).order
    scores = {d: (d * 7) % 101 for d in rs.order}
    assert set(order) == set(rs.order) and [scores[d] for d in order] == sorted(scores.values(), reverse=True)
    assert len(srt.data["cards"]) == 10 and srt.text.count("file://") == 10 and "★" in srt.text
    assert f"(j_01 {rs.count}/{rs.count})" in srt.text.splitlines()[-1]
    flt = verbs.filter_(ctx, "rs_02", Criteria(skills=["elixir"]))
    sub = ctx.session().get(flt.data["set"])
    assert sub.judgment == "j_01" and sub.order == [d for d in order if d in set(sub.order)]
    assert "★" in flt.text
    # the judgment travels to the subset, so nobody is left to score
    again = verbs.cards(ctx, "rs_03")
    assert again.data["shown"] == 0 and "all judged under j_01" in again.text
    # a new criterion: `cards --new` prints every card again, without the old stars
    fresh = verbs.cards(ctx, "rs_03", new=True)
    assert fresh.data["shown"] == sub.count and "★" not in fresh.text
    payload = {"criterion": "leadership", "judge": "pytest", "scores": [{"id": ids[d], "score": 50, "note": "n"} for d in sub.order]}
    ctx.session().scores_path.write_text(json.dumps(payload))
    assert verbs.score(ctx, "rs_03", source="scores.json").data["judgment"] == "j_02"
    with pytest.raises(ResumesError) as e:
        verbs.sort_(ctx, "rs_01", "judgment", judgment="j_09")
    assert e.value.code == "UNKNOWN_JUDGMENT"
    verbs.search(ctx, Criteria(skills=["cobol"]))
    with pytest.raises(ResumesError) as e:
        verbs.sort_(ctx, None, "judgment")
    assert e.value.code == "NO_JUDGMENT"


def test_paging_verbs(ctx):
    verbs.search(ctx, Criteria(skills=["elixir"]))
    n1 = verbs.next_(ctx)
    assert n1.data["start"] == 1 and n1.data["end"] == 10 and n1.data["state"]["page"] == 1
    verbs.next_(ctx)
    p3 = verbs.page(ctx, 3)
    assert p3.data["start"] == 21 and p3.data["state"]["cursor"] == 30
    assert verbs.prev_(ctx).data["start"] == 11
    assert verbs.top(ctx, 3).data["end"] == 3
    for _ in range(20):
        last = verbs.next_(ctx)
        if last.data.get("end_of_set"):
            break
    assert last.data["end_of_set"] is True and "end of set" in last.text
    assert ctx.session().resolve(None).cursor == 56 and last.data["state"]["page"] == 6
    with pytest.raises(ResumesError) as e:
        verbs.page(ctx, 99)
    assert e.value.code == "NO_SUCH_PAGE"
    with pytest.raises(ResumesError):
        verbs.back(ctx)
    verbs.filter_(ctx, "rs_01", Criteria(rate_max=80))
    assert verbs.back(ctx).data["set"] == "rs_01"
    assert verbs.use(ctx, "2").data["set"] == "rs_02"
    tree = verbs.sets(ctx).text
    assert "rs_01" in tree and "└ rs_02" in tree and "← current" in tree


def test_show_vocab_and_index_version_notice(ctx):
    out = verbs.search(ctx, Criteria(skills=["elixir"]))
    first = ctx.session().get(out.data["set"]).order[0]
    doc_id = ctx.index.ids([first])[first]
    card = verbs.show(ctx, doc_id)
    assert card.text.startswith(doc_id + " · ") and card.data["link"].startswith("file://")
    full = verbs.show(ctx, str(first), full=True)
    assert full.text.startswith("# ") and full.text.rstrip().endswith(card.data["link"])
    with pytest.raises(ResumesError) as e:
        verbs.show(ctx, doc_id, contact=True)
    assert e.value.code == "CONTACT_DISABLED"
    v = verbs.vocab(ctx, "phoenix")
    assert v.data["terms"][0]["slug"] == "phoenix-framework" and "implies: elixir" in v.text
    # a set made against another index version says so
    rs = ctx.session().get(out.data["set"])
    rs.index_version = "older"
    ctx.session().save_cursor(rs, 0)
    assert "(index updated since this set was made)" in verbs.next_(ctx).text
    # a document that vanished from the index is dropped from the page with a notice, never silently
    rs = ctx.session().get(out.data["set"])
    rs.order[0] = 9_999_999
    ctx.session().save_cursor(rs, 0)
    assert "removed from the index" in verbs.next_(ctx).text


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs an embedder")
def test_text_fallback_says_so(ctx):
    out = verbs.search(ctx, Criteria(topics=["moved data nightly between systems"]))
    assert 'no topic/skill matched "moved data nightly between systems" — text search' in out.text
    assert out.data["count"] > 0 and out.data["unresolved"] == ["moved data nightly between systems"]
