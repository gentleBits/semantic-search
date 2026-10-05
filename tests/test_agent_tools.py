"""Tools that describe the data: places, countries, empty results and field coverage (skipped without the index)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentic_search import config as config_mod
from agentic_search.index.store import current_path
from agentic_search.query import view
from agentic_search.query.search import Criteria, places

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from agentic_search.web.app import create_app  # noqa: E402
from tests.testroot import TEST_ROOT

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
pytestmark = pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built")


class Api:
    def __init__(self, tmp_path):
        cfg = config_mod.load(TEST_ROOT)
        cfg.query.sessions = tmp_path / "sessions"
        self.cfg = cfg
        self.http = TestClient(create_app(cfg))
        self.sid = self.http.post("/api/sessions").json()["session"]["id"]

    def state(self):
        return self.http.get(f"/api/sessions/{self.sid}/state").json()["state"]

    def tool(self, name, **args):
        r = self.http.post(f"/api/sessions/{self.sid}/tool", json={"name": name, "args": args})
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def api(tmp_path):
    return Api(tmp_path)


def test_criteria_take_one_place_or_several():
    assert places(None) == [] and places("  Krakow,  Poland ") == ["Krakow, Poland"] and places(["Poland", "Spain", "Poland", " "]) == ["Poland", "Spain"]
    one = Criteria(location="Poland")
    assert one.location == ["Poland"] and one.to_args() == {"location": "Poland"}, "one place is written in the older single-value form: saved filters and replays keep their keys"
    several = Criteria(location=["Spain", "Poland"])
    assert several.has_constraints and several.to_args() == {"location": ["Spain", "Poland"]}
    assert Criteria.from_args(several.to_args()).location == ["Spain", "Poland"]
    assert view.condition_key(None, Criteria(location=["Spain", "Poland"])) == view.condition_key(None, Criteria(location=["poland", "SPAIN"])), "any of them: the order means nothing"
    assert view.condition_key(None, Criteria(location=["Poland"])) == view.condition_key(None, Criteria(location="Poland"))


def test_config_describes_the_fields_and_says_where_the_conversations_live(api):
    c = api.http.get("/api/config").json()
    f = c["fields"]
    assert f["people"] == 3026
    assert f["location"]["stated"] == 709 and f["location"]["examples"][:3] == ["Krakow, Poland", "Madrid, Spain", "Hamburg, Germany"]
    assert any("," not in e for e in f["location"]["examples"]), "a location without a country shows too"
    assert f["rate"] == {"stated": 390, "estimated": 2636, "unknown": 0} and f["years"] == {"known": 2965, "unknown": 61}
    assert f["availability"]["stated"] == 409 and [v for v, _ in f["availability"]["values"]][:4] == ["2w", "3m", "now", "1m"]
    assert all(re.fullmatch(r"now|\d+[dwm]|other", v) for v, _ in f["availability"]["values"]), "the labels the facets use, the rest folded into other"

    assert f["remote"] == {"stated": 280}
    assert c["sessions_dir"] == str(api.cfg.query.sessions.resolve())


def test_the_overview_lists_countries_and_how_many_state_a_location(api):
    told = api.tool("search", topic=["data pipelines"])["result"]
    assert "where: location stated for 121 of 165 · open to remote 74" in told
    assert "country or state (the last part of the location): Germany 14 · France 11 · Spain 11 · Netherlands 10" in told
    assert "cities: Madrid 8 · Zurich 8 · Hamburg 7" in told
    o = api.state()["overview"]
    assert o["location_stated"] == 121 and o["countries"][0] == {"name": "Germany", "count": 14} and len(o["countries"]) > 20
    assert api.tool("overview")["result"].count("country or state") == 1


def test_a_location_filter_takes_several_places(api):
    api.tool("search", topic=["data pipelines"])
    out = api.tool("filter", location=["Poland", "Spain", "Germany"], said="in Poland, Spain or Germany")
    assert out["result"].startswith("32 people (before: 165)\nadded f2 location: Poland or Spain or Germany — 131 match it alone")
    assert out["events"][0]["step"] == {"sign": "+", "text": "added location: Poland or Spain or Germany", "detail": "165 → 32"}
    f = api.state()["filters"][1]
    assert (f["kind"], f["value"], f["said"], f["args"]["location"]) == ("location", "Poland or Spain or Germany", "in Poland, Spain or Germany", ["Poland", "Spain", "Germany"])
    alone = {p: api.tool("search", location=[p])["events"][-1]["state"]["set"]["count"] for p in ("Poland", "Spain", "Germany")}
    assert alone == {"Poland": 42, "Spain": 38, "Germany": 51} and sum(alone.values()) == 131, "any of them: the union of the three"
    assert api.tool("search", location="Poland")["events"][-1]["state"]["filters"][0]["value"] == "Poland", "a string still works"


def test_a_place_is_a_whole_word_of_the_location(api):
    from agentic_search.query.search import place_matches

    assert place_matches("Poland", "Krakow, Poland") and place_matches("d.c.", "Washington, D.C.") and place_matches("new york", "New York, NY")
    assert not place_matches("NY", "Hamburg, Germany") and not place_matches("Pol", "Krakow, Poland") and not place_matches("Poland", None)
    ny = api.tool("search", location=["NY"])["events"][-1]["state"]["set"]["count"]
    germany = api.tool("search", location=["Germany"])["events"][-1]["state"]["set"]["count"]
    assert 0 < ny < 10 < germany, f"NY matches New York, not Germany: {ny} vs {germany}"
    assert api.tool("search", location=["D.C."])["events"][-1]["state"]["set"]["count"] == 1
    api.tool("search", topic=["data pipelines"])
    api.tool("filter", location=["Germany"])
    s = api.state()
    assert s["set"]["count"] == 14 == s["overview"]["countries"][0]["count"], "the overview's country count is what the filter gives"


def test_a_filter_that_matches_nobody_tells_the_model_what_the_set_holds(api):
    api.tool("search", topic=["data pipelines"])
    told = api.tool("filter", location=["EU"])["result"]
    lines = told.split("\n")
    assert lines[0] == "0 people (before: 165)" and lines[1] == "added f2 location: EU — 0 match it alone"
    assert lines[2] == "nobody is left: location: EU matched nobody of the 165 the other filters leave"
    assert lines[3] == "the 165 people without it:"
    assert "  where: location stated for 121 of 165 · open to remote 74" in lines
    assert any(ln.startswith("  country or state (the last part of the location): Germany 14 · France 11") for ln in lines)
    assert "  rate per hour: median €75 (quartiles €61–€93) · estimated for 59 of 165" in lines
    assert lines[-2] == "the screen offers to remove: location: EU → 165"
    assert lines[-1].startswith("[screen] 0 people")
