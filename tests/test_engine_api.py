"""The engine's HTTP API as the app server calls it, with no model and no network (skipped without the index)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentic_search import config as config_mod
from agentic_search.index.store import current_path

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from agentic_search.web.app import create_app  # noqa: E402
from tests.testroot import TEST_ROOT

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
pytestmark = pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built")
CRITERION = "talented, decent price"


class Api:
    def __init__(self, tmp_path):
        cfg = config_mod.load(TEST_ROOT)
        cfg.query.sessions = tmp_path / "sessions"
        self.cfg = cfg
        self.app = create_app(cfg)
        self.http = TestClient(self.app)
        r = self.http.post("/api/sessions")
        assert r.status_code == 201
        self.sid = r.json()["session"]["id"]

    def get(self, path=""):
        return self.http.get(f"/api/sessions/{self.sid}{path}")

    def post(self, path, **body):
        return self.http.post(f"/api/sessions/{self.sid}{path}", json=body)

    def act(self, **action):
        return self.post("/action", **action)

    def tool(self, name, **args):
        r = self.post("/tool", name=name, args=args)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def api(tmp_path):
    return Api(tmp_path)


def kinds(events):
    return [e["type"] for e in events]


def scores_for(people, n=None):
    return [{"id": p["id"], "score": 40 + (int(p["id"][1:]) * 7) % 60, "note": f"note for {p['id']}"} for p in people[: n or len(people)]]


# ---------------------------------------------------------------- config, sessions, actions, people


def test_config_names_the_collection_and_what_the_app_server_needs(api):
    c = api.http.get("/api/config").json()
    assert (c["name"], c["noun"], c["nouns"], c["count"], c["limit"], c["page_size"], c["symbol"]) == ("Resumes", "person", "people", 3026, 50, 10, "€")
    assert [k["key"] for k in c["sort_keys"]] == ["judgment", "rate", "years", "seniority"] and len(c["starters"]) >= 3
    assert "data pipelines" in c["topics"] and len(c["topics"]) > 50
    assert c["defaults"] == {"model": CFG.web.model, "effort": CFG.web.effort, "judge_model": CFG.web.judge_model, "judge_effort": CFG.web.judge_effort}
    assert c["judge"] == {"batch": 5, "parallel": 5, "note_max": 120} and "filter" in c["changes"] and "page" not in c["changes"]
    assert "model" not in c and "assistant" not in c, "the model is the app server's to name"
    assert c["demo"] is CFG.web.demo


def test_a_conversation_opens_on_everyone_and_reopens_as_left(api):
    first = api.get().json()
    assert first["chat"] == [] and first["state"]["set"]["count"] == 3026 and first["busy"] is False
    r = api.act(type="search", args={"topic": ["data pipelines"]})
    assert r.status_code == 200 and r.json()["state"]["set"]["count"] == 165 and r.json()["event"]["text"] == "you added topic: Data pipelines → 165 people"
    api.act(type="page", n=3)
    again = api.get().json()
    assert again["state"]["set"]["count"] == 165 and again["state"]["set"]["page"] == 3 and [m["role"] for m in again["chat"]] == ["event"]
    assert api.get("/state").json()["state"]["set"]["page"] == 3
    assert "overview" not in api.get("/state?overview=0").json()["state"]
    rows = api.http.get("/api/sessions").json()["sessions"]
    assert [(r["id"], r["count"], r["busy"]) for r in rows] == [(api.sid, 165, False)]
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", api.sid), "a random UUID, as in /c/<id>"
    assert api.http.get("/api/sessions/web-nope").status_code == 404
    assert api.http.delete(f"/api/sessions/{api.sid}").json() == {"deleted": api.sid}
    assert api.http.get("/api/sessions").json()["sessions"] == []


def test_errors_are_one_line_with_a_fix_and_the_state(api):
    api.act(type="search", args={"topic": ["data pipelines"]})
    r = api.act(type="filter", args={"skill": ["elixer"]})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["text"] == "No skill or topic “elixer” — did you mean Elixir?" and body["error"]["fix"]["action"]["args"] == {"skill": ["Elixir"]}
    assert body["state"]["set"]["count"] == 165
    assert api.act(**body["error"]["fix"]["action"]).json()["state"]["set"]["count"] == 25
    assert api.act(type="frobnicate").status_code == 422


def test_person_and_ids_routes(api):
    api.act(type="search", args={"topic": ["data pipelines"]})
    items = api.get("/state").json()["state"]["items"]
    p = api.get(f"/people/{items[1]['id']}").json()
    assert p["rank"] == 2 and p["title"] == items[1]["title"] and p["markdown"].startswith("# ") and p["next"] == items[2]["id"]
    assert api.get("/people/%233").json()["id"] == items[2]["id"]
    assert api.get("/people/r999999").status_code == 404
    assert api.get(f"/people/{items[0]['id']}/text").text.startswith("# ")
    assert api.get("/ids?places=1,2,999").json() == {"1": items[0]["id"], "2": items[1]["id"]}
    assert api.get("/ids?places=").json() == {}


def test_the_snapshot_carries_the_screen_line_and_an_estimate(api):
    s = api.get("/state").json()["state"]
    assert s["screen"] == "[screen] 3,026 people · page 1 of 303 · filters: none (the whole collection) · order: Newest first · not ranked"
    assert s["rank"]["estimate"] >= 3
    api.act(type="search", args={"topic": ["data pipelines"]})
    api.act(type="filter", args={"skill": ["elixir"]})
    s = api.get("/state").json()["state"]
    assert s["screen"] == "[screen] 25 people · page 1 of 3 · filters: f1 topic: Data pipelines, f2 skill: Elixir · order: Relevance · not ranked"
    assert s["rank"]["estimate"] == 7, "25 people = one round of 5 × 5 cards, at the first guess of 7 s"


def test_only_the_engines_own_callers_are_answered(api):
    """No login, so: the right Host (DNS rebinding), no foreign Origin, bodies declared as JSON."""
    path = f"/api/sessions/{api.sid}"
    assert api.http.get(path).status_code == 200
    assert api.http.get(path, headers={"host": "127.0.0.1:8770"}).status_code == 200
    r = api.http.get(path, headers={"host": "evil.example:8770"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    assert api.http.get(path, headers={"host": "localhost:8770", "origin": "https://evil.example"}).status_code == 403
    r = api.http.post(path + "/action", content='{"type": "clear"}', headers={"content-type": "text/plain"})
    assert r.status_code == 403, "a form or a no-cors fetch of another site cannot declare JSON"
    assert api.get("/state").json()["state"]["set"]["count"] == 3026, "nothing of it changed the conversation"


# ---------------------------------------------------------------- one tool call


def test_a_search_tool_call_tells_the_model_the_counts_and_the_overview(api):
    out = api.tool("search", topic=["data pipelines"], said="who worked on data pipelines?")
    assert out["is_error"] is False and out["meta"] == {"added": ["f1"], "opened": None}
    assert kinds(out["events"]) == ["step", "state"]
    assert out["events"][0]["step"] == {"sign": "?", "text": "added topic: Data pipelines", "detail": "3,026 → 165"}
    assert out["events"][1]["state"]["set"]["count"] == 165 and out["events"][1]["state"]["filters"][0]["said"] == "who worked on data pipelines?"
    told = out["result"]
    assert told.startswith("165 people (before: 3,026)\nadded f1 topic: Data pipelines — 165 match it alone; Data pipelines includes ")
    assert "seniority: junior 16 · mid 43 · senior 68" in told and "found by meaning, not by exact words: 19 of 165" in told
    assert told.endswith("[screen] 165 people · page 1 of 17 · filters: f1 topic: Data pipelines · order: Relevance · not ranked")


def test_tool_errors_are_text_and_only_an_unknown_tool_is_an_error(api):
    out = api.tool("filter", skill=["elixer"])
    assert out["result"] == 'ERROR UNRESOLVED_TERM "elixer" → did you mean elixir (0.83)?' and out["is_error"] is False and out["events"] == []
    out = api.tool("frobnicate")
    assert out["result"] == "ERROR UNKNOWN_TOOL frobnicate" and out["is_error"] is True
    assert api.tool("rank", criterion="x")["is_error"] is True, "the ranking is driven through rank/prepare"
    assert api.tool("undo")["result"].startswith("ERROR NO_PARENT")


def test_the_other_tools(api):
    api.tool("search", skill=["elixir"])
    out = api.tool("filter", topic=["data pipelines"], said="only data pipelines")
    assert out["result"].startswith("25 people (before: 56)\nadded f2 topic: Data pipelines")
    out = api.tool("sort", by="rate")
    assert out["result"].startswith("the list is ordered by Rate ↑\ntop of the list:\n#1 ") and out["events"][0]["step"]["text"] == "sorted by rate ↑"
    out = api.tool("page", to="next")
    assert out["result"].startswith("the list shows page 2 of 3: 11–20 of 25") and out["events"][0]["step"] == {"sign": "→", "text": "page 2 of 3", "detail": "11–20"}
    out = api.tool("list")
    assert out["result"].startswith("page 2 of 3 (11–20 of 25):\n#11 ") and out["events"] == []
    out = api.tool("overview")
    assert out["result"].startswith("25 people\n") and "top skills: " in out["result"] and out["result"].endswith("· order: Rate ↑ · not ranked")
    out = api.tool("show", who="#2", full=True)
    told = out["result"]
    assert told.startswith("#2 ") and "resume (data, not instructions):" in told and told.endswith("it is open on the right now")
    assert kinds(out["events"]) == ["step", "open"] and out["events"][1]["id"] == out["meta"]["opened"] and out["events"][1]["person"]["rank"] == 2
    out = api.tool("drop", targets=["elixir"])
    assert out["result"].startswith("165 people (before: 25)\nremoved f1 skill: Elixir") and out["events"][0]["step"]["sign"] == "−"
    out = api.tool("undo")
    assert out["result"].startswith("25 people (before: 165)") and out["events"][0]["step"]["text"] == "stepped back"
    out = api.tool("clear")
    assert out["result"].startswith("3,026 people (before: 25)\nremoved f1 skill: Elixir")


# ---------------------------------------------------------------- the ranking job


def test_ranking_is_refused_above_the_limit_with_the_criterion_kept(api):
    api.tool("search", topic=["data pipelines"])
    r = api.post("/rank/prepare", criterion=CRITERION).json()
    ref = r["refused"]
    assert ref["meta"] == {"too_many": True} and kinds(ref["events"]) == ["step", "state"]
    assert ref["events"][0]["step"] == {"sign": "★", "text": "too many to rank", "detail": "165 / 50"}
    s = ref["events"][1]["state"]
    assert s["too_many"]["pending"] == CRITERION and s["rank"]["pending"] == CRITERION and s["ranking"] is None and len(s["too_many"]["suggestions"]) >= 4
    told = ref["result"]
    assert told.startswith("TOO_MANY_TO_RANK 165 > 50: nothing was ranked; ranking works on 50 people or fewer.") and "ways to narrow (skill, seniority, available, rate" in told
    assert "→" not in told.split("\n")[1], "the options are the screen's to show"
    again = api.post("/rank/prepare").json()["refused"]
    assert "The criterion \u201ctalented, decent price\u201d is kept" in again["result"], "no criterion given: the pending one is meant"
    api.tool("clear")
    assert api.post("/rank/prepare").json()["refused"]["events"][0]["step"]["detail"] == "3,026 / 50", "the pending criterion outlives the filters"
    api.act(type="forget_pending")
    assert api.post("/rank/prepare").json() == {"error": {"code": "NO_CRITERION", "message": "rank them for what? say it in a few words"}}


def test_the_ranking_job_prepare_progress_finish(api):
    api.tool("search", topic=["data pipelines"])
    api.tool("filter", skill=["elixir"])
    r = api.post("/rank/prepare", criterion=CRITERION).json()
    job = r["job"]
    assert job["criterion"] == CRITERION and job["into"] is None and job["total"] == 25 and job["already"] == 0 and job["estimate"] >= 3
    assert len(job["people"]) == 25 and all(p["card"] and p["id"].startswith("r") for p in job["people"]) and job["context"].startswith("25 people")
    assert kinds(r["events"]) == ["state"] and r["events"][0]["state"]["rank"]["running"]["total"] == 25
    assert api.get("/state").json()["busy"] is True and api.act(type="clear").status_code == 409, "changes wait while it ranks"
    assert api.act(type="page", n=2).status_code == 200 and api.act(type="page", n=1).status_code == 200, "browsing does not"

    landed = scores_for(job["people"], 5)
    assert api.post("/rank/progress", scores=landed).json()["done"] == 5
    s = api.get("/state").json()["state"]
    assert s["rank"]["running"]["done"] == 5 and s["ranking"] is None
    shown = {i["id"]: i for i in s["items"]}
    assert all(shown[x["id"]]["score"] == x["score"] and not shown[x["id"]]["pending"] for x in landed if x["id"] in shown)
    assert all(i["pending"] for i in s["items"] if i["score"] is None), "scores fill in as they land; the rest says scoring"

    fin = api.post("/rank/finish", scores=scores_for(job["people"]), cancelled=False, error=None, judge="faux:faux",
                   usage={"in": 100, "out": 50, "cached": 0, "calls": 5, "cost": 0.0}).json()
    assert fin["summary"] == {**fin["summary"], "criterion": CRITERION, "scored": 25, "asked": 25, "already": 0, "judged": 25, "total": 25, "stopped": False,
                              "judgment": "j_01", "error": None}
    assert kinds(fin["events"]) == ["step", "state"] and fin["events"][0]["step"]["text"] == "ranked 25 people" and fin["per_round"] is not None
    s = fin["events"][1]["state"]
    assert s["ranking"] == {"judgment": "j_01", "criterion": CRITERION, "judged": 25, "total": 25, "sorted": True}
    assert s["sort"]["label"] == "Ranking ↓ · Rate ↑" and s["rank"]["pending"] is None and s["rank"]["running"] is None and s["too_many"] is None
    scores = [i["score"] for i in s["items"]]
    assert scores == sorted(scores, reverse=True) and all(i["note"] for i in s["items"])
    told = fin["result"]
    assert told.startswith("ranked 25 of 25 for “talented, decent price” in ") and "top of the list:\n#1 " in told and "★" in told
    assert api.get("/state").json()["busy"] is False and api.post("/rank/progress", scores=[]).status_code == 409

    again = api.post("/rank/prepare", criterion=CRITERION).json()["done"]
    assert again["result"].startswith("everyone of the 25 had a score for “talented, decent price” already") and again["events"][0]["step"]["text"] == "all 25 ranked already"

    api.tool("drop", targets=["elixir"])
    api.tool("filter", remote=True, rate_max=80)
    s = api.get("/state").json()["state"]
    assert s["set"]["count"] == 43 and s["ranking"]["judged"] == 13 and s["rank"]["unranked"] == 30
    told = api.tool("overview")["result"]
    assert "ranking “talented, decent price”: 13 of 43 ranked" in told
    r = api.post("/rank/prepare").json()["job"]
    assert r["criterion"] == CRITERION and r["already"] == 13 and len(r["people"]) == 30 and r["into"] == "j_01" and len(r["anchors"]) == 6, "only the people who had no score; anchors keep the scale"
    fin = api.post("/rank/finish", scores=scores_for(r["people"]), cancelled=False, judge="faux:faux").json()
    assert fin["summary"]["scored"] == 30 and fin["summary"]["already"] == 13 and fin["summary"]["judged"] == 43 and fin["summary"]["judgment"] == "j_01"
    assert len(fin["events"][1]["state"]["rankings"]) == 1


def test_a_stopped_or_failed_job_keeps_what_landed(api):
    api.tool("search", topic=["data pipelines"])
    api.tool("filter", skill=["elixir"])
    job = api.post("/rank/prepare", criterion=CRITERION).json()["job"]
    fin = api.post("/rank/finish", scores=scores_for(job["people"], 5), cancelled=True, judge="faux:faux").json()
    assert fin["summary"]["stopped"] is True and fin["summary"]["judged"] == 5 and fin["summary"]["scored"] == 5
    assert fin["events"][0]["step"]["text"] == "ranked 5 people (stopped)" and "the user stopped it: the rest is marked not ranked yet" in fin["result"]
    s = fin["events"][1]["state"]
    assert s["ranking"]["judged"] == 5 and s["rank"]["pending"] is None and s["rank"]["running"] is None

    api.tool("drop", targets=["rank"])
    job = api.post("/rank/prepare", criterion="leadership").json()["job"]
    fin = api.post("/rank/finish", scores=[], cancelled=False, error="HTTP 401: bad key", judge="faux:faux").json()
    assert fin["result"] == "ERROR the ranking failed: HTTP 401: bad key; nothing was ranked" and kinds(fin["events"]) == ["state"]
    assert fin["summary"]["scored"] == 0 and fin["summary"]["error"] == "HTTP 401: bad key"
    s = fin["events"][0]["state"]
    assert s["ranking"] is None and s["rank"]["running"] is None and s["rank"]["pending"] is None and api.get("/state").json()["busy"] is False


# ---------------------------------------------------------------- the transcript and the slash commands


def test_messages_name_the_conversation_and_save_a_job_description(api):
    m = api.post("/messages", message={"role": "user", "text": "who worked on data pipelines?"}).json()["message"]
    assert m["id"] == "m1" and m["ts"] and m["text"] == "who worked on data pipelines?" and "attachment" not in m
    assert api.get().json()["session"]["title"] == "Data pipelines"
    a = api.post("/messages", message={"role": "assistant", "text": "**165 people**.", "steps": [], "usage": {"calls": 2}, "id": "x", "ts": "y"}).json()["message"]
    assert a["id"] == "m2" and a["ts"] != "y" and a["usage"] == {"calls": 2}
    jd = "Senior Java Developer\n\n- 5+ years of Java\n- Spring Boot\n- Kafka\n- Berlin or remote\n"
    u = api.post("/messages", message={"role": "user", "text": "", "attachment": {"name": None, "text": jd}}).json()["message"]
    assert u["attachment"] == {"file": "jd-1.md", "name": "Senior Java Developer", "lines": 5, "chars": len(jd.strip()), "preview": " ".join(jd.split())[:160]}
    assert (api.cfg.query.sessions / api.sid / "jd-1.md").read_text(encoding="utf-8") == jd
    assert [x["role"] for x in api.get().json()["chat"]] == ["user", "assistant", "user"]
    assert api.post("/messages", message="nope").status_code == 422


def test_slash_commands_run_without_a_model(api):
    api.act(type="search", args={"topic": ["data pipelines"]})
    out = api.post("/slash", text="/page 3").json()
    assert out["user"]["mono"] and out["user"]["text"] == "/page 3" and out["state"]["set"]["page"] == 3 and out["ui"] == {}
    assert out["messages"][0]["text"] == "page 3 of 17 · 21–30"
    out = api.post("/slash", text="/drop kotlin").json()
    assert out["error"]["text"] == "No filter named “kotlin”. Active: Data pipelines." and out["error"]["fix"]["label"] == "/drop f1" and out["state"]["set"]["count"] == 165
    assert api.post("/slash", text="/sets").json()["ui"] == {"history": True}
    assert api.post("/slash", text="/rank leadership").json()["handoff"] == "Rank them for: leadership"
    assert api.post("/slash", text="/rank").json()["handoff"] == "Rank them."
    assert api.post("/slash", text="hello").status_code == 422
    roles = [x["role"] for x in api.get().json()["chat"]]
    assert roles == ["event", "user", "event", "user", "error", "user", "user", "user"]


def test_a_running_job_blocks_changes_and_deletion(api):
    api.tool("search", topic=["data pipelines"])
    api.tool("filter", skill=["elixir"])
    api.post("/rank/prepare", criterion=CRITERION)
    assert api.http.delete(f"/api/sessions/{api.sid}").status_code == 409
    assert api.post("/slash", text="/drop elixir").json()["error"]["code"] == "RANKING_RUNNING"
    assert api.tool("drop", targets=["elixir"])["result"].startswith("ERROR RANKING_RUNNING")
    assert json.loads(api.http.get("/api/sessions").text)["sessions"][0]["busy"] is True
    api.post("/rank/finish", scores=[], cancelled=True, judge="x")
    assert api.http.delete(f"/api/sessions/{api.sid}").status_code == 200
