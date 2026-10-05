"""The web UI's snapshot and panel steps on the golden scenario, 3,026 → 165 → 25 → 165 → 43 (skipped without the index)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agentic_search import config as config_mod
from agentic_search.errors import ResumesError
from agentic_search.index.store import current_path
from agentic_search.query import verbs
from agentic_search.query.verbs import Context
from agentic_search.web import actions, state
from agentic_search.web.store import WebSession, list_web_sessions
from tests.testroot import TEST_ROOT, TIME_FACTOR

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
pytestmark = pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built")
CRITERION = "talented, decent price"


@pytest.fixture
def cfg(tmp_path):
    c = config_mod.load(TEST_ROOT)
    c.query.sessions = tmp_path / "sessions"
    return c


@pytest.fixture
def web(cfg):
    return WebSession.create(cfg.query.sessions, "web-t")


@pytest.fixture
def ctx(cfg, web):
    return Context(cfg, session_id=web.id)


def snap(ctx, web, **kw):
    return state.snapshot(ctx, web, **kw)


def do(ctx, web, **action):
    return actions.act(ctx, web, action)


def judge(ctx, web, *, score=lambda d: (d * 7) % 101, into=None):
    """Scores for everyone of the current set who has none yet, recorded the way the ranking job records them."""
    session, rs = verbs._current(ctx)
    jid, scores = verbs._judgment_for(ctx, session, rs, ignore_off=True)
    todo = [d for d in rs.order if scores.get(d, (None, ""))[0] is None] if into else list(rs.order)
    ids = ctx.index.ids(todo)
    payload = {"criterion": CRITERION, "judge": "pytest", "scores": [{"id": ids[d], "score": score(d), "note": f"note {d}"} for d in todo]}
    verbs.score(ctx, rs.id, text=json.dumps(payload), into=into)
    return actions.sort(ctx, web, "judgment,rate")


# ---------------------------------------------------------------- the snapshot


def test_first_open_is_everyone_newest_first(ctx, web):
    s = snap(ctx, web)
    assert s["set"]["count"] == 3026 and s["set"]["everyone"] and s["set"]["page"] == 1 and s["set"]["pages"] == 303
    assert s["filters"] == [] and s["sort"]["label"] == "Newest first" and s["ranking"] is None
    assert s["rank"] == {"limit": 50, "possible": False, "unranked": 3026, "pending": None, "running": None, "estimate": s["rank"]["estimate"]} and s["rank"]["estimate"] > 100
    assert [i["rank"] for i in s["items"]] == list(range(1, 11))
    assert [i["doc_no"] for i in s["items"]] == sorted((i["doc_no"] for i in s["items"]), reverse=True), "newest first"
    assert s["history"] == [{"id": "rs_01", "n": 1, "parent": None, "op": "all", "sign": "", "label": "start · everyone", "count": 3026,
                             "ranked": False, "current": True}]
    assert s["overview"]["rate"]["estimated"] > 2000 and s["too_many"] is None and "zero" not in s


def test_rows_carry_separate_fields(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    s = snap(ctx, web)
    first = s["items"][0]
    assert first["title"] == "Junior Data Engineer" and first["years_label"] == "3y" and first["rate_label"] == "€45"
    assert first["location"] == "Barcelona" and first["remote"] is True and first["avail"] == "3m" and first["avail_label"] == "3 mo"
    assert first["used"] and isinstance(first["listed"], list) and first["did"] and first["skills"].count(" · ") <= 4
    assert first["score"] is None and first["note"] is None and first["pending"] is False
    talend = next(i for i in s["items"] if i["title"].startswith("Talend ETL Developer"))
    assert talend["rate_estimated"] and talend["rate_label"].startswith("~€") and talend["location"] is None and talend["avail"] is None
    assert talend["title"] == "Talend ETL Developer - Tata Consultancy Services", "long titles are sent whole; the UI cuts them"


def test_question_then_filter_gives_chips_with_counts(ctx, web):
    out = do(ctx, web, type="search", args={"topic": ["data pipelines"]}, said="who worked on data pipelines?")
    s = out["state"]
    assert s["set"]["count"] == 165 and s["set"]["delta"] == [3026, 165] and s["sort"]["label"] == "Relevance"
    f1 = s["filters"][0]
    assert (f1["kind"], f1["value"], f1["alone"], f1["without"], f1["said"]) == ("topic", "Data pipelines", 165, 3026, "who worked on data pipelines?")
    assert f1["by_meaning"] == 19 and s["overview"]["direct"] == 146 and s["overview"]["by_meaning"] == 19
    assert f1["terms"][0]["expanded"] and f1["terms"][0]["more"] > 0
    assert out["event"]["text"] == "you added topic: Data pipelines → 165 people"

    out = do(ctx, web, type="filter", args={"skill": ["elixir"]})
    s = out["state"]
    assert s["set"]["count"] == 25 and s["set"]["delta"] == [165, 25] and s["rank"]["possible"]
    assert [(f["kind"], f["value"], f["alone"], f["without"]) for f in s["filters"]] == [("topic", "Data pipelines", 165, 56), ("skill", "Elixir", 56, 165)]
    assert "Phoenix Framework" in s["filters"][1]["terms"][0]["expanded"]
    assert [h["label"] for h in s["history"]] == ["start · everyone", "topic: Data pipelines", "skill: Elixir"]
    assert [h["sign"] for h in s["history"]] == ["", "?", "+"]


def test_several_terms_become_one_chip_each_in_one_step(ctx, web):
    out = do(ctx, web, type="search", args={"skill": ["elixir", "kubernetes"], "remote": True})
    s = out["state"]
    assert [(f["kind"], f["value"]) for f in s["filters"]] == [("skill", "Elixir"), ("skill", "Kubernetes"), ("", "remote")]
    assert len(s["history"]) == 2, "one request is one step"
    assert out["event"]["text"].startswith("you added skill: Elixir, skill: Kubernetes, remote → ")
    n = s["set"]["count"]
    one = do(ctx, web, type="drop", targets=[s["filters"][1]["id"]])["state"]
    assert [(f["kind"], f["value"]) for f in one["filters"]] == [("skill", "Elixir"), ("", "remote")] and one["set"]["count"] >= n
    # "any of" is one condition: one chip
    any_ = do(ctx, web, type="search", args={"skill": ["elixir", "kubernetes"], "mode": "any"})["state"]
    assert [(f["kind"], f["value"]) for f in any_["filters"]] == [("skills", "Elixir or Kubernetes")]


def test_a_word_that_does_not_resolve_changes_nothing(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    before = snap(ctx, web)
    with pytest.raises(ResumesError) as e:
        do(ctx, web, type="filter", args={"skill": ["kubernetes", "elixer"], "remote": True})
    assert e.value.code == "UNRESOLVED_TERM"
    after = snap(ctx, web)
    assert after["set"]["id"] == before["set"]["id"] and len(after["history"]) == len(before["history"])
    words = actions.friendly(ctx, web, e.value)
    assert words["text"] == "No skill or topic “elixer” — did you mean Elixir?"
    assert words["fix"]["action"] == {"type": "filter", "args": {"skill": ["Elixir"]}}
    fixed = actions.act(ctx, web, words["fix"]["action"])["state"]
    assert fixed["set"]["count"] == 25


def test_too_many_to_rank_names_filters_that_deliver(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    assert snap(ctx, web)["too_many"] is None, "no banner until a ranking was asked for"
    web.set_pending(CRITERION)
    s = snap(ctx, web)
    t = s["too_many"]
    assert (t["count"], t["limit"], t["pending"]) == (165, 50, CRITERION) and 4 <= len(t["suggestions"]) <= 8
    assert len({x["kind"] for x in t["suggestions"][:4]}) == 4, "the first few are of different kinds"
    for sug in t["suggestions"]:
        assert 2 <= sug["count"] <= 50
        got = do(ctx, web, type="filter", args=sug["args"])["state"]
        assert got["set"]["count"] == sug["count"], f"{sug}: promised {sug['count']}, delivered {got['set']['count']}"
        assert got["too_many"] is None and got["rank"]["possible"] and got["rank"]["pending"] == CRITERION
        do(ctx, web, type="undo")


def test_ranked_then_partly_ranked(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    do(ctx, web, type="filter", args={"skill": ["elixir"]})
    judge(ctx, web)
    s = snap(ctx, web)
    assert s["ranking"] == {"judgment": "j_01", "criterion": CRITERION, "judged": 25, "total": 25, "sorted": True}
    assert s["sort"]["label"] == "Ranking ↓ · Rate ↑" and s["rank"]["unranked"] == 0
    scores = [i["score"] for i in s["items"]]
    assert scores == sorted(scores, reverse=True) and all(i["note"] for i in s["items"])
    assert s["history"][-1]["sign"] == "★" and s["history"][-1]["label"] == f"ranked · {CRITERION}" and s["history"][-1]["ranked"]
    kept = {i["id"]: i["score"] for i in s["items"]}

    out = do(ctx, web, type="drop", targets=["elixir"])
    s = out["state"]
    assert s["set"]["count"] == 165 and s["ranking"]["judged"] == 25 and s["rank"]["unranked"] == 140
    assert out["event"]["text"] == "you removed skill: Elixir → 165 people · 25 keep their ★"
    assert {i["id"]: i["score"] for i in s["items"]} == kept, "the ranked stay on top with their scores"
    page3 = do(ctx, web, type="page", n=3)["state"]
    assert page3["set"]["start"] == 21 and [i["score"] is None for i in page3["items"]] == [False] * 5 + [True] * 5

    s = do(ctx, web, type="filter", args={"remote": True, "rate_max": 80})["state"]
    assert s["set"]["count"] == 43 and s["ranking"]["judged"] == 13 and s["rank"]["unranked"] == 30 and s["rank"]["possible"]
    rate = next(f for f in s["filters"] if f["kind"] == "rate")
    assert rate["value"] == "≤ €80/h" and rate["alone"] > 1000 and rate["without"] > 43 and "estimated" in rate and rate["unknown"]["what"] == "rate"
    judge(ctx, web, into="j_01")
    s = snap(ctx, web)
    assert s["ranking"]["judged"] == 43 and s["rank"]["unranked"] == 0
    assert {i["id"]: i["score"] for i in s["items"] if i["id"] in kept} == {k: v for k, v in kept.items() if k in {i["id"] for i in s["items"]}}


def test_ranking_off_on_and_a_second_criterion(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    do(ctx, web, type="filter", args={"skill": ["elixir"]})
    judge(ctx, web)
    off = do(ctx, web, type="rank_off")["state"]
    assert off["ranking"] is None and off["ranking_off"] and off["sort"]["label"] == "Relevance" and all(i["score"] is None for i in off["items"])
    assert off["rankings"] == [{"judgment": "j_01", "criterion": CRITERION, "covers": 25}]
    on = do(ctx, web, type="rank_on", judgment="j_01")
    assert on["state"]["ranking"]["judged"] == 25 and on["state"]["ranking"]["sorted"] and on["event"]["text"] == f"you turned on the ranking “{CRITERION}”"
    session, rs = verbs._current(ctx)
    ids = ctx.index.ids(rs.order)
    verbs.score(ctx, rs.id, text=json.dumps({"criterion": "leadership", "judge": "pytest", "scores": [{"id": ids[d], "score": d % 100, "note": "x"} for d in rs.order]}))
    s = do(ctx, web, type="rank_on", judgment="j_02")["state"]
    assert s["ranking"]["criterion"] == "leadership" and [r["judgment"] for r in s["rankings"]] == ["j_01", "j_02"]


def test_sort_page_undo_history(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    out = do(ctx, web, type="sort", by="rate,years")
    s = out["state"]
    rates = [i["rate"] for i in s["items"]]
    assert rates == sorted(rates) and s["sort"]["label"] == "Rate ↑ · Years ↓" and out["event"]["text"] == "you sorted by rate ↑ · years ↓"
    assert s["sort"]["keys"] == [{"key": "rate", "dir": "asc"}, {"key": "years", "dir": "desc"}] and s["sort"]["base_label"] == "Relevance"
    base = do(ctx, web, type="sort", by="relevance")["state"]
    assert base["sort"]["label"] == "Relevance" and base["items"][0]["title"] == "Junior Data Engineer"
    p = do(ctx, web, type="page", n=17)["state"]
    assert (p["set"]["page"], p["set"]["start"], p["set"]["end"], len(p["items"])) == (17, 161, 165, 5)
    assert "event" not in do(ctx, web, type="prev"), "paging leaves no line in the chat"
    assert snap(ctx, web)["set"]["page"] == 16
    with pytest.raises(ResumesError) as e:
        do(ctx, web, type="page", n=40)
    words = actions.friendly(ctx, web, e.value)
    assert words["text"] == "There is no page 40 — the list has 17." and words["fix"]["action"] == {"type": "page", "n": 17}

    back = do(ctx, web, type="undo")
    assert back["state"]["sort"]["label"] == "Rate ↑ · Years ↓" and back["event"]["text"] == "you stepped back → 165 people"
    do(ctx, web, type="undo")
    top = do(ctx, web, type="undo")["state"]
    assert top["set"]["count"] == 3026 and top["set"]["delta"] == [165, 3026], "undo crosses a new question (the engine's `back` stops there)"
    with pytest.raises(ResumesError) as e:
        do(ctx, web, type="undo")
    assert actions.friendly(ctx, web, e.value)["text"] == "Nothing to undo — this is the first step."
    jump = do(ctx, web, type="use", set="rs_03")
    assert jump["state"]["sort"]["label"] == "Rate ↑ · Years ↓" and jump["event"]["text"] == "you went to step 3 → 165 people"
    assert [h["current"] for h in jump["state"]["history"]] == [False, False, True, False]


def test_zero_results_say_which_filter_emptied_it(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"], "remote": True})
    do(ctx, web, type="filter", args={"skill": ["elixir"]})
    s = do(ctx, web, type="filter", args={"location": "Atlantis"})["state"]
    assert s["set"]["count"] == 0 and s["items"] == [] and s["set"]["page"] == 0 and s["overview"] == {"count": 0, "direct": 0, "by_meaning": 0}
    z = s["zero"]
    culprit = next(f for f in s["filters"] if f["culprit"])
    assert z["culprit"] == culprit["id"] and culprit["kind"] == "location" and z["filters"] == 4
    assert z["options"][0]["id"] == culprit["id"] and z["options"][0]["without"] == culprit["without"] > 0
    back = do(ctx, web, type="drop", targets=[z["culprit"]])["state"]
    assert back["set"]["count"] == z["options"][0]["without"] and "zero" not in back


def test_unknown_values_can_be_included(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    s = do(ctx, web, type="filter", args={"min_years": 5})["state"]
    years = next(f for f in s["filters"] if f["kind"] == "years")
    assert years["value"] == "≥ 5" and years["unknown"] == {"count": 7, "included": False, "what": "years"}
    n = s["set"]["count"]
    out = do(ctx, web, type="unknown", id=years["id"], include=True)
    s2 = out["state"]
    assert s2["set"]["count"] == n + 7 and len(s2["filters"]) == 2 and out["event"]["text"] == f"you included people with unknown years → {n + 7} people"
    assert next(f for f in s2["filters"] if f["kind"] == "years")["unknown"]["included"] is True


def test_person_detail(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    do(ctx, web, type="filter", args={"skill": ["elixir"]})
    judge(ctx, web)
    s = snap(ctx, web)
    out = do(ctx, web, type="open", id="#2")
    p = out["person"]
    assert p["id"] == s["items"][1]["id"] and p["rank"] == 2 and p["of"] == 25 and p["prev"] == s["items"][0]["id"] and p["next"] == s["items"][2]["id"]
    assert p["score"] == s["items"][1]["score"] and p["criterion"] == CRITERION and p["place"] == 2
    assert p["markdown"].startswith("# ") and "\nname:" not in p["markdown"] and "---" not in p["markdown"][:5], "no front matter: it holds the name"
    assert set(s["items"][1]["used"]) <= set(p["used"]) and out["ui"] == {"open": p["id"]}
    assert out["event"]["text"] == f"you opened #2 · {p['title']}"
    by_id = state.person(ctx, web, p["id"])
    assert by_id["rank"] == 2 and by_id["title"] == p["title"]
    with pytest.raises(ResumesError) as e:
        state.person(ctx, web, "#99")
    assert actions.friendly(ctx, web, e.value)["text"] == "There is no #99 — the list has 25."


# ---------------------------------------------------------------- slash commands, the transcript, sessions


def test_slash_commands_take_no_model(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"], "remote": True})
    r = actions.slash(ctx, web, "/page 3")
    assert r["state"]["set"]["page"] == 3 and r["messages"][-1]["text"].startswith("page 3 of ")
    r = actions.slash(ctx, web, "/next")
    assert r["state"]["set"]["page"] == 4
    r = actions.slash(ctx, web, "/filters")
    assert r["messages"][-1]["role"] == "info" and r["messages"][-1]["text"].splitlines()[0].startswith("f1 · topic: Data pipelines · 165 alone · without it: ")
    with pytest.raises(ResumesError) as e:
        actions.slash(ctx, web, "/drop kotlin")
    words = actions.friendly(ctx, web, e.value)
    assert words["text"] == "No filter named “kotlin”. Active: Data pipelines, remote." and words["fix"]["label"] == "/drop f2"
    r = actions.slash(ctx, web, "/drop remote")
    assert r["state"]["set"]["count"] == 165 and r["messages"][0]["text"] == "you removed remote → 165 people"
    assert actions.slash(ctx, web, "/sets")["ui"] == {"history": True}
    assert actions.slash(ctx, web, "/show #1")["ui"]["open"] == r["state"]["items"][0]["id"]
    assert actions.slash(ctx, web, "/rank leadership") == {"handoff": "Rank them for: leadership"}
    r = actions.slash(ctx, web, "/clear-filters")
    assert r["state"]["set"]["count"] == 3026 and r["state"]["set"]["everyone"]
    assert actions.slash(ctx, web, "/page 10")["state"]["set"]["start"] == 91
    with pytest.raises(ResumesError) as e:
        actions.slash(ctx, web, "/frobnicate")
    assert e.value.code == "UNKNOWN_COMMAND"


def test_conversations_are_kept_and_listed(cfg, ctx, web):
    web.name_from("who worked on data pipelines?")
    assert web.title == "Data pipelines"
    web.append({"role": "user", "text": "who worked on data pipelines?"})
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    again = WebSession.open(cfg.query.sessions, web.id)
    assert [m["role"] for m in again.chat] == ["user", "event"] and [m["id"] for m in again.chat] == ["m1", "m2"]
    s = state.snapshot(Context(cfg, session_id=web.id), again)
    assert s["set"]["count"] == 165 and s["session"]["title"] == "Data pipelines", "reopened exactly as left"
    other = WebSession.create(cfg.query.sessions)
    rows = list_web_sessions(cfg.query.sessions)
    assert {r["id"] for r in rows} == {web.id, other.id}
    assert next(r for r in rows if r["id"] == web.id)["count"] == 165
    Context(cfg, session_id="cli-session").session(create=True)
    assert len(list_web_sessions(cfg.query.sessions)) == 2, "sessions of the CLI are not conversations"
    fname = again.save_jd("# Senior Java Developer\n\n- 5+ years of Java\n- Spring, Hibernate\n")
    assert fname == "jd-1.md" and again.jd_name(fname) == "Senior Java Developer" and (again.dir / fname).is_file()


def test_a_running_ranking_blocks_changes_not_browsing(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    do(ctx, web, type="filter", args={"skill": ["elixir"]})
    session, rs = verbs._current(ctx)
    job = {"running": True, "set": rs.id, "criterion": CRITERION, "done": 2, "total": 25, "seconds": 3,
           "scores": {rs.order[0]: (88, "strong"), rs.order[3]: (52, "junior")}, "waiting": set(rs.order) - {rs.order[0], rs.order[3]}}
    with pytest.raises(ResumesError) as e:
        actions.act(ctx, web, {"type": "drop", "targets": ["elixir"]}, job=job)
    assert e.value.code == "RANKING_RUNNING"
    s = actions.act(ctx, web, {"type": "page", "n": 1}, job=job)["state"]
    assert s["rank"]["running"] == {"criterion": CRITERION, "done": 2, "total": 25, "seconds": 3, "set": rs.id}
    assert [(i["score"], i["pending"]) for i in s["items"][:4]] == [(88, False), (None, True), (None, True), (52, False)], "scores fill in as they land"
    assert s["ranking"] is None and s["sort"]["label"] == "Relevance", "the order switches when the ranking is done"


def test_steps_are_fast(ctx, web):
    do(ctx, web, type="search", args={"topic": ["data pipelines"]})
    snap(ctx, web)
    timings = {}
    for name, action in [("filter", {"type": "filter", "args": {"skill": ["elixir"]}}), ("drop", {"type": "drop", "targets": ["elixir"]}),
                         ("sort", {"type": "sort", "by": "rate"}), ("page", {"type": "page", "n": 5}), ("clear", {"type": "clear"}),
                         ("page 10 of all", {"type": "page", "n": 10}), ("undo", {"type": "undo"})]:
        t = time.perf_counter()
        do(ctx, web, **action)
        timings[name] = (time.perf_counter() - t) * 1000
    assert max(timings.values()) < 150 * TIME_FACTOR, timings      # the target is 100 ms; the margin is for a busy machine
