"""The golden scenario's eleven turns as real `resumes` processes; fixture counts within ±15 % (skipped without the index)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pyroaring import BitMap

from agentic_search import config as config_mod
from agentic_search.index.embed import token_counter
from agentic_search.index.store import current_path, open_current
from tests.testroot import TEST_ROOT, TIME_FACTOR

ROOT = Path(__file__).resolve().parents[2]
CFG = config_mod.load(TEST_ROOT)
pytestmark = pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built")

GT = json.loads((ROOT / "data" / "fixture" / "ground_truth.json").read_text(encoding="utf-8"))["docs"]
LATENCY_MS = 300 * TIME_FACTOR  # per CLI call (no call embeds a query here)
TOKEN_TARGET = 20_000       # everything this tool adds to the agent's context over the eleven turns
TOKEN_CEILING = 21_000      # enforced: twelve pages of ~1.15k tokens just miss the target; this guards against growth
PAGE = 10
LIMIT = CFG.query.max_cards
count_tokens = token_counter(CFG.index.embedder)
SKILL_MD_TOKENS = count_tokens((ROOT / ".agents" / "skills" / "resumes" / "SKILL.md").read_text(encoding="utf-8"))
CRITERION = "talented; decent price = rate ≤ p50 (€75/h)"


def cli() -> list[str]:
    exe = Path(sys.executable).parent / "resumes"
    return [str(exe)] if exe.is_file() else [sys.executable, "-m", "agentic_search.cli"]


@dataclass
class Call:
    args: list[str]
    stdout: str
    stderr: str
    code: int
    ms: float

    @property
    def tokens(self) -> int:
        # links carry the clone's folder: counted as if it were a typical one, so the budget does not depend on it
        text = (self.stdout + self.stderr).replace(TEST_ROOT.resolve().as_uri(), "file:///home/you/semantic-search")
        return count_tokens(text.replace(str(TEST_ROOT.resolve()), "/home/you/semantic-search"))

    @property
    def state(self) -> str:
        return self.stdout.rstrip().splitlines()[-1]

    @property
    def set_id(self) -> str:
        return re.match(r"— (rs_\d+) ", self.state).group(1)

    @property
    def link_lines(self) -> list[str]:
        return [ln for ln in self.stdout.splitlines() if re.match(r"^\d+\. ", ln)]

    @property
    def card_ids(self) -> list[str]:
        """Ids of the cards to judge (the block after `cards:`; anchors come before it)."""
        body = self.stdout.split("\ncards:\n", 1)[1] if "\ncards:\n" in self.stdout else ""
        return [ln.split(" · ", 1)[0] for ln in body.splitlines() if re.match(r"^r\d{6} · ", ln)]

    @property
    def stars(self) -> dict[str, str]:
        """link → `★score note` for every line of the links block."""
        out = {}
        for ln in self.link_lines:
            m = re.search(r" · (★\S+ [^·]*?) · (?:\+\d+ versions? · )?(file://\S+)$", ln)
            if m:
                out[m.group(2)] = m.group(1)
        return out


@dataclass
class Scenario:
    sid: str
    sdir: Path
    calls: list[Call] = field(default_factory=list)
    conversation: list[Call] = field(default_factory=list)       # the calls of the eleven turns
    turns: dict[str, Call] = field(default_factory=dict)
    written: list[str] = field(default_factory=list)              # the scores files the agent would have written

    def run(self, *args: str, expect: int = 0, turn: str | None = None, sid: str | None = None, check: bool = False) -> Call:
        """`check`: a call the test makes to look at the state; not a turn of the conversation (no tokens counted)."""
        env = {**os.environ, "RESUMES_SESSION": sid or self.sid}
        t0 = time.perf_counter()
        r = subprocess.run(cli() + list(args), capture_output=True, text=True, env=env, cwd=TEST_ROOT)
        c = Call(list(args), r.stdout, r.stderr, r.returncode, (time.perf_counter() - t0) * 1000)
        if sid is None:
            self.calls.append(c)
            if not check:
                self.conversation.append(c)
        assert c.code == expect, f"{args}: exit {c.code}\n{c.stdout}\n{c.stderr}"
        if turn:
            self.turns[turn] = c
        return c

    def set(self, rs_id: str, sid: str | None = None) -> dict:
        return json.loads((self.sdir.parent / (sid or self.sid) / f"{rs_id}.json").read_text(encoding="utf-8"))

    def set_of(self, turn: str) -> dict:
        return self.set(self.turns[turn].set_id)

    def judge(self, cards: Call, *, judgment: str | None = None) -> list[str]:
        """Write the scores file the way SKILL.md prescribes (compact, one entry per line) for the cards just printed."""
        m = re.search(r"write scores to (\S+)", cards.stdout.splitlines()[0])
        assert m, "cards must print where to write scores"
        path = Path(m.group(1))
        assert path.parent == self.sdir, "scores live in the session directory"
        ids = cards.card_ids
        head = {"criterion": CRITERION, "judge": "golden test (generated)", **({"judgment": judgment} if judgment else {})}
        scores = [{"id": i, "score": int(hashlib.sha256(i.encode()).hexdigest()[:8], 16) % 101, "note": "generated"} for i in ids]
        text = "{" + ", ".join(f'"{k}": {json.dumps(v, ensure_ascii=False)}' for k, v in head.items()) + ', "scores": [\n' \
            + ",\n".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in scores) + "\n]}\n"
        path.write_text(text, encoding="utf-8")
        self.written.append(text)
        return ids


@pytest.fixture(scope="module")
def scenario():
    sid = f"golden-{secrets.token_hex(3)}"
    sc = Scenario(sid, CFG.query.sessions / sid)
    side = [f"{sid}-b", f"{sid}-c", f"{sid}-d"]
    try:
        # 1 — "who worked on data pipelines?"
        sc.run("search", "--topic", "data pipelines", "--said", "who worked on data pipelines?", turn="1")
        # 2 — "show them" · /page 10
        sc.run("next", turn="2a")
        sc.run("page", "10", turn="2b")
        # 3 — "the talented ones at a decent price": too many people; the engine is the backstop
        sc.run("cards", expect=1, turn="3")
        # 4 — "only those who know elixir"
        sc.run("filter", "--skill", "elixir", "--said", "only those who know elixir", turn="4")
        # 5 — "now the talented ones at a decent price"
        c = sc.run("cards", turn="5cards")
        sc.judged_first = sc.judge(c)
        sc.run("score", "--from", "scores.json", turn="5score")
        sc.run("sort", "--by", "judgment,rate", turn="5")
        # 6 — /next, three times
        sc.run("next", turn="6a")
        sc.run("next", turn="6b", check=True)
        sc.run("next", turn="6c", check=True)
        sc.snapshot_5 = sc.set_of("5")
        # 7 — "remove the elixir filter"
        sc.run("drop", "f2", turn="7")
        sc.run("next", turn="7next", check=True)
        # 8 — "only remote, under €80"
        sc.run("filter", "--remote", "--rate-max", "80", turn="8")
        sc.run("filters", turn="8filters", check=True)
        # 9 — "rank them": only the people not judged yet
        c = sc.run("cards", turn="9cards")
        sc.judged_second = sc.judge(c, judgment="j_01")
        sc.run("score", "--from", "scores.json", turn="9score")
        sc.run("sort", "--by", "judgment,rate", turn="9")
        sc.run("top", "50", turn="9all", check=True)
        # 10 — "add elixir back"
        sc.run("filter", "--skill", "elixir", turn="10")
        sc.run("next", turn="10next", check=True)
        # 11 — /clear-filters · /page 10
        sc.run("clear", turn="11")
        sc.run("page", "10", turn="11page")

        # side sessions: never part of the conversation, only there to check it
        sc.run("page", "10", sid=side[0], turn="fresh")                                        # browse before any search
        sc.run("search", "--skill", "elixir", sid=side[1])                                     # the same filters, added in another order
        sc.run("filter", "--rate-max", "80", "--no-show", sid=side[1])
        sc.run("filter", "--topic", "data pipelines", "--remote", "--no-show", sid=side[1], turn="permuted")
        sc.run("search", "--topic", "data pipelines", "--remote", "--rate-max", "80", sid=side[2], turn="oneshot")   # … and in one call
        sc.sides = side
        yield sc
    finally:
        for s in [sid, *side]:
            shutil.rmtree(CFG.query.sessions / s, ignore_errors=True)


@pytest.fixture(scope="module")
def truth():
    con = open_current(CFG.index.out)
    fixture = dict(con.execute("SELECT source_id, doc_no FROM docs WHERE source = 'fixture'").fetchall())
    elixir_bm = BitMap.deserialize(bytes(con.execute("SELECT strong_bm FROM postings p JOIN terms t USING (term_id) WHERE t.slug = 'elixir'").fetchone()[0]))
    n_docs = con.execute("SELECT count(*) FROM docs WHERE NOT deleted").fetchone()[0]
    con.close()
    return {
        "fixture": set(fixture.values()),
        "pipelines": {fixture[s] for s, g in GT.items() if g["pipeline"]},
        "elixir_pipelines": {fixture[s] for s, g in GT.items() if g["pipeline"] and g["elixir"] != "none"},
        "controls": {fixture[s] for s, g in GT.items() if g["negative_control"]},
        "elixir_bm": elixir_bm,
        "n_docs": n_docs,
    }


def within(n: int, expected: int, tol: float = 0.15) -> bool:
    return (1 - tol) * expected <= n <= (1 + tol) * expected


# ---------------------------------------------------------------- the eleven turns


def test_turn1_count_and_facets_only(scenario, truth):
    c = scenario.turns["1"]
    rs = scenario.set("rs_01")
    assert c.stdout.startswith("rs_01 · ") and "topic data-pipelines (expanded:" in c.stdout.splitlines()[0]
    assert "cards:" not in c.stdout and "file://" not in c.stdout and not c.link_lines, "a count question gets no list"
    assert re.search(r"^rate €/h  p25 \d+ · p50 \d+ · p75 \d+", c.stdout, re.M), "the agent needs rate p50 for the ranking turn"
    assert f"ranking needs ≤ {LIMIT} people" in c.stdout and "resumes cards" not in c.stdout, "the hint never sends the agent to rank 165 people"
    assert c.state == f"— rs_01 · {rs['count']} · page 0/{-(-rs['count'] // PAGE)} · f1 topic=data-pipelines"
    members = set(rs["order"])
    fixture_members = members & truth["fixture"]
    assert within(len(fixture_members), len(truth["pipelines"])), (len(fixture_members), len(truth["pipelines"]))
    assert len(fixture_members & truth["pipelines"]) / len(truth["pipelines"]) >= 0.85, "recall on the fixture"
    assert rs["parent"] is None and rs["count"] == len(rs["order"]) > LIMIT and rs["filters"] == ["f1"]


def test_turn2_browse_a_big_set(scenario):
    a, b = scenario.turns["2a"], scenario.turns["2b"]
    n = scenario.set("rs_01")["count"]
    assert a.stdout.startswith(f"rs_01 · {n} people · 1–10 of {n}") and [ln.split(".")[0] for ln in a.link_lines] == [str(i) for i in range(1, 11)]
    assert b.stdout.startswith(f"rs_01 · {n} people · 91–100 of {n}") and [ln.split(".")[0] for ln in b.link_lines] == [str(i) for i in range(91, 101)]
    assert "page 10/" in b.state and not any("★" in ln for ln in a.link_lines + b.link_lines)
    assert scenario.set("rs_01")["cursor"] >= 100, "paging moved the cursor of the same set; it made no new one"


def test_turn3_ranking_a_big_set_is_refused(scenario):
    c = scenario.turns["3"]
    n = scenario.set("rs_01")["count"]
    lines = c.stderr.strip().splitlines()
    assert c.stdout == "" and len(lines) == 3
    assert lines[0] == f"TOO_MANY_TO_RANK {n} > {LIMIT} — tell the user the set is too big to rank and ask them to narrow it first"
    assert lines[1].startswith(f"narrow to ≤ {LIMIT} with:  skill ") and " | seniority " in lines[1] and "rate ≤ €" in lines[1]
    assert lines[2] == "listing works at any size: `resumes next`"
    assert scenario.turns["5score"].stdout.startswith("j_01 · "), "nothing was judged in this turn: the first judgment is turn 5's"


def test_turn4_filter(scenario, truth):
    c = scenario.turns["4"]
    rs1, rs2 = scenario.set("rs_01"), scenario.set("rs_02")
    assert rs2["parent"] == "rs_01" and rs2["op"] == "filter" and rs2["filters"] == ["f1", "f2"]
    assert rs2["order"] == [d for d in rs1["order"] if d in truth["elixir_bm"]], "the order of rs_01, restricted to the elixir bitmap, element by element"
    members = set(rs2["order"])
    assert within(len(members & truth["fixture"]), len(truth["elixir_pipelines"])) and rs2["count"] <= LIMIT
    assert not (members & truth["controls"]), "University of Phoenix / Phoenix, AZ never count as Elixir"
    assert c.stdout.startswith(f"rs_02 · {rs2['count']} people · skill elixir")
    assert len(c.link_lines) == PAGE, "the parent was shown → page 1"
    assert c.state == f"— rs_02 · {rs2['count']} · page 1/3 · f1 topic=data-pipelines · f2 skill=elixir"
    f2 = json.loads((scenario.sdir / "filters" / "f2.json").read_text(encoding="utf-8"))
    assert f2["said"] == "only those who know elixir" and f2["label"] == "skill=elixir" and f2["count"] > rs2["count"]


def test_turn5_rank_the_short_list(scenario):
    cards, score, sort = scenario.turns["5cards"], scenario.turns["5score"], scenario.turns["5"]
    rs2, rs3 = scenario.set("rs_02"), scenario.set("rs_03")
    assert len(cards.card_ids) == rs2["count"] == len(set(cards.card_ids)), "one card per member"
    assert cards.stdout.splitlines()[0].startswith(f"rs_02 · {rs2['count']} cards · rate p50 €") and "anchors" not in cards.stdout
    assert score.stdout.startswith(f"j_01 · rs_02 · {rs2['count']} scored")
    assert (scenario.sdir / "judgments.parquet").is_file() and (scenario.sdir / "scores" / "j_01.json").is_file()
    assert rs3["parent"] == "rs_02" and rs3["judgment"] == "j_01" and set(rs3["order"]) == set(rs2["order"])
    assert rs3["sort"] == ["judgment:desc", "rate:asc"] and rs3["filters"] == ["f1", "f2"]
    con = open_current(CFG.index.out)
    ids = dict(con.execute("SELECT doc_no, id FROM docs WHERE doc_no IN (SELECT unnest($1::INTEGER[]))", [rs3["order"]]).fetchall())
    rates = dict(con.execute("SELECT doc_no, rate FROM profile WHERE doc_no IN (SELECT unnest($1::INTEGER[]))", [rs3["order"]]).fetchall())
    con.close()
    by_id = {i: int(hashlib.sha256(i.encode()).hexdigest()[:8], 16) % 101 for i in scenario.judged_first}
    keys = [(-by_id[ids[d]], rates[d]) for d in rs3["order"]]
    assert keys == sorted(keys), "score ↓ then rate ↑"
    assert len(sort.card_ids) == PAGE and len(sort.link_lines) == PAGE and all("★" in ln for ln in sort.link_lines), "exactly one page"
    assert sort.state == f"— rs_03 · {rs3['count']} · page 1/3 · f1 topic=data-pipelines · f2 skill=elixir · sorted judgment↓ rate↑ (j_01 {rs3['count']}/{rs3['count']})"


def test_turn6_next_is_mechanical_and_the_cursor_persists(scenario):
    n1, n2, n3 = scenario.turns["6a"], scenario.turns["6b"], scenario.turns["6c"]
    count = scenario.snapshot_5["count"]
    assert [ln.split(".")[0] for ln in n1.link_lines] == [str(i) for i in range(11, min(20, count) + 1)]
    assert [ln.split(".")[0] for ln in n2.link_lines] == [str(i) for i in range(21, min(30, count) + 1)]
    assert "end of set" in n3.stdout and not n3.link_lines and n3.code == 0
    assert "page 2/" in n1.state and "page 3/" in n2.state and "end of set" in n3.state
    assert scenario.snapshot_5["cursor"] == count, "the cursor is persisted between processes"
    assert n1.stdout.startswith(f"rs_03 · {count} people · 11–20 of {count}")


def test_turn7_removing_the_filter_brings_everyone_back(scenario):
    c = scenario.turns["7"]
    rs1, rs3, rs4 = scenario.set("rs_01"), scenario.set("rs_03"), scenario.set_of("7")
    assert c.stdout.startswith(f"{rs4['id']} · {rs1['count']} people · removed f2 skill=elixir")
    assert set(rs4["order"]) == set(rs1["order"]) and rs4["filters"] == ["f1"] and rs4["parent"] == "rs_03" and rs4["op"] == "drop"
    assert rs4["order"][: rs3["count"]] == rs3["order"], "the people judged so far stay on top, in their order"
    assert len(c.link_lines) == PAGE and c.stars == scenario.turns["5"].stars, "page 1 is the ranked page of before, star for star"
    assert c.state.endswith(f"f1 topic=data-pipelines · sorted judgment↓ rate↑ (j_01 {rs3['count']}/{rs1['count']})")
    assert [ln.split(".")[0] for ln in scenario.turns["7next"].link_lines] == [str(i) for i in range(11, 21)]


def test_turn8_another_filter(scenario):
    c, listing = scenario.turns["8"], scenario.turns["8filters"]
    rs = scenario.set_of("8")
    assert rs["filters"] == ["f1", "f3", "f4"] and 0 < rs["count"] <= LIMIT < scenario.set("rs_01")["count"]
    assert c.stdout.startswith(f"{rs['id']} · {rs['count']} people · rate-max=80 --remote")
    lines = listing.stdout.splitlines()
    n1 = scenario.set("rs_01")["count"]
    assert lines[0].startswith(f'f1  topic=data-pipelines · {n1} alone · without it: ') and lines[0].endswith('"who worked on data pipelines?"')
    assert re.fullmatch(r"f3  rate-max=80 · \d+ alone · without it: \d+", lines[1]) and re.fullmatch(r"f4  remote · \d+ alone · without it: \d+", lines[2])
    assert lines[3].startswith(f'rank  j_01 "{CRITERION}" · ') and f" of {rs['count']} judged · sorted judgment↓ rate↑" in lines[3]
    assert lines[4] == "remove one: `resumes drop f4` · all: `resumes clear` · the ranking: `resumes drop rank`"


def test_turn9_only_the_new_people_are_judged(scenario):
    cards, score, sort = scenario.turns["9cards"], scenario.turns["9score"], scenario.turns["9"]
    rs8, rs9 = scenario.set_of("8"), scenario.set_of("9")
    first, second = set(scenario.judged_first), set(scenario.judged_second)
    con = open_current(CFG.index.out)
    ids = dict(con.execute("SELECT doc_no, id FROM docs WHERE doc_no IN (SELECT unnest($1::INTEGER[]))", [rs8["order"]]).fetchall())
    con.close()
    members = {ids[d] for d in rs8["order"]}
    assert second == members - first and second and (members & first), "exactly the members not judged in turn 5"
    head = cards.stdout.splitlines()[0]
    assert head.startswith(f"{rs8['id']} · {len(second)} cards ({len(members & first)} of {rs8['count']} already judged) · rate p50 €")
    assert f'same criterion as j_01 "{CRITERION}"' in head and head.endswith('scores.json with "judgment":"j_01"')
    assert cards.stdout.count("★") == 6 and "anchors (judged before under j_01" in cards.stdout, "three highest and three lowest, one line each, with their scores"
    assert cards.stdout.split("anchors (")[1].split("\ncards:\n")[0].count("\n") == 7, "header + six one-line anchors + the --new hint"
    assert score.stdout.startswith(f"j_01 · {rs8['id']} · {len(second)} scored · added to j_01: {rs8['count']} of {rs8['count']} judged")
    assert rs9["judgment"] == "j_01" and set(rs9["order"]) == set(rs8["order"]) and rs9["filters"] == rs8["filters"]
    assert sort.state.endswith(f"sorted judgment↓ rate↑ (j_01 {rs9['count']}/{rs9['count']})")
    assert len(scenario.turns["9all"].link_lines) == rs9["count"] and all("★" in ln for ln in scenario.turns["9all"].link_lines)


def test_turn10_the_first_filter_again(scenario, truth):
    c = scenario.turns["10"]
    rs9, rs10 = scenario.set_of("9"), scenario.set_of("10")
    assert rs10["filters"] == ["f1", "f3", "f4", "f2"], "elixir is the filter it was: f2, not a new one"
    assert rs10["order"] == [d for d in rs9["order"] if d in truth["elixir_bm"]] and 0 < rs10["count"] < rs9["count"]
    assert sorted(p.name for p in (scenario.sdir / "filters").glob("f*.json")) == ["f1.json", "f2.json", "f3.json", "f4.json"]
    assert all("★" in ln for ln in c.link_lines) and c.state.endswith(f"(j_01 {rs10['count']}/{rs10['count']})")


def test_turn11_clear_and_browse_everything(scenario, truth):
    c, p = scenario.turns["11"], scenario.turns["11page"]
    rs = scenario.set_of("11")
    assert rs["filters"] == [] and rs["sort"] == [] and rs["ranking_off"] is True and rs["op"] == "clear"
    assert within(rs["count"], truth["n_docs"], 0.02) and rs["count"] <= truth["n_docs"], "everyone; near-duplicate versions count once"
    assert rs["order"] == sorted(rs["order"], reverse=True), "newest first"
    assert c.stdout.startswith(f"{rs['id']} · {rs['count']} people · all CVs (filters and ranking removed)")
    assert [ln.split(".")[0] for ln in p.link_lines] == [str(i) for i in range(91, 101)] and not any("★" in ln for ln in p.link_lines)
    assert p.state == f"— {rs['id']} · {rs['count']} · page 10/{-(-rs['count'] // PAGE)} · no filters (all CVs)"


# ---------------------------------------------------------------- the eight pass criteria


def test_criterion1_round_trip(scenario):
    rs1 = scenario.set("rs_01")
    scenario.run("use", scenario.turns["7"].set_id, check=True)
    back = scenario.run("sort", "--by", "relevance", check=True)
    rs = scenario.set(back.set_id)
    assert rs["order"] == rs1["order"], "remove the filter and the ranking: rs_01 again, element by element"


def test_criterion2_members_do_not_depend_on_the_order_of_adding(scenario):
    main = scenario.set_of("8")
    permuted = scenario.set(scenario.turns["permuted"].set_id, scenario.sides[1])
    # the side session added elixir as well; take it out of the comparison by adding it to the main view (turn 10)
    assert set(permuted["order"]) == set(scenario.set_of("10")["order"])
    assert len(permuted["filters"]) == 4 and set(main["order"]) >= set(permuted["order"])


def test_criterion3_adding_equals_computing_from_scratch(scenario):
    oneshot = scenario.set(scenario.turns["oneshot"].set_id, scenario.sides[2])
    step = scenario.set_of("8")
    assert set(oneshot["order"]) == set(step["order"])
    assert oneshot["order"] == [d for d in scenario.set("rs_01")["order"] if d in set(step["order"])], "unranked: the order of the first filter"


def test_criterion4_scores_stick(scenario):
    t5 = {**scenario.turns["5"].stars, **scenario.turns["6a"].stars, **scenario.turns["6b"].stars}
    assert len(t5) == scenario.snapshot_5["count"]
    later = {}
    for t in ("7", "9all", "10", "10next"):
        for link, star in scenario.turns[t].stars.items():
            later.setdefault(link, set()).add(star)
    assert all(len(v) == 1 for v in later.values()), "one person, one criterion, one score"
    assert all(later[link] == {star} for link, star in t5.items() if link in later)
    assert set(t5) & set(later), "people of turn 5 do show up again"
    assert not (set(scenario.judged_first) & set(scenario.judged_second)), "no person is in two `cards` outputs for one criterion"


def test_criterion5_every_suggestion_delivers_its_count(scenario):
    sid = scenario.sides[2]
    r = scenario.run("cards", "rs_01", "--json", expect=1, check=True)
    data = json.loads(r.stdout)
    assert data["error"] == "TOO_MANY_TO_RANK" and data["data"]["limit"] == LIMIT
    sugg = data["data"]["suggestions"]
    assert len(sugg) >= 8
    scenario.run("search", "--topic", "data pipelines", sid=sid)
    base = scenario.run("sets", "--json", sid=sid)
    root = [s["id"] for s in json.loads(base.stdout)["sets"] if s["op"] == "search"][-1]
    for s in sugg:
        call = s["call"].split()[2:]                     # "resumes filter --skill kubernetes" → the flags
        out = json.loads(scenario.run("filter", root, *call, "--no-show", "--json", sid=sid).stdout)
        assert out["count"] == s["count"] <= LIMIT, s
    ok = scenario.run("cards", "--json", sid=sid)        # the last of them can be ranked
    assert json.loads(ok.stdout)["shown"] == sugg[-1]["count"]


def test_criterion6_browse_before_any_search(scenario, truth):
    c = scenario.turns["fresh"]
    assert c.stdout.startswith("rs_01 · ") and "91–100 of" in c.stdout.splitlines()[0] and len(c.link_lines) == PAGE
    assert c.state.endswith("no filters (all CVs)") and c.stderr == ""


def test_criterion7_speed(scenario):
    slow = [(c.args, round(c.ms)) for c in scenario.calls if c.ms > LATENCY_MS]
    assert not slow, f"calls over {LATENCY_MS} ms: {slow}"
    # removing is as cheap as paging; one shot each, with headroom for a busy machine
    paging = sorted(c.ms for c in scenario.calls if c.args[0] in ("next", "page"))
    for verb in ("drop", "clear"):
        ms = next(c.ms for c in scenario.calls if c.args[0] == verb)
        assert ms <= max(150 * TIME_FACTOR, 2 * paging[len(paging) // 2]), f"{verb}: {ms:.0f} ms"


def test_criterion8_token_budget(scenario):
    outputs = sum(c.tokens for c in scenario.conversation)
    scores = sum(count_tokens(t) for t in scenario.written)
    total = outputs + scores + SKILL_MD_TOKENS
    print(f"\ntokens: outputs {outputs} + scores {scores} + SKILL.md {SKILL_MD_TOKENS} = {total} (target {TOKEN_TARGET}, ceiling {TOKEN_CEILING})")
    assert total <= TOKEN_CEILING, f"outputs {outputs} + scores {scores} + SKILL.md {SKILL_MD_TOKENS} = {total} > {TOKEN_CEILING}"
    big = [(c.args, c.tokens) for c in scenario.conversation if c.tokens > 2500]
    assert not big, f"no output of the conversation is a wall of text: {big}"


# ---------------------------------------------------------------- links and output contract


def test_every_link_opens_the_full_resume(scenario):
    links = {ln.rsplit(" · ", 1)[1] for c in scenario.calls for ln in c.link_lines}
    assert links
    for url in links:
        assert url.startswith("file://")
        p = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
        assert p.is_file() and p.read_text(encoding="utf-8").lstrip().startswith("---"), url
    assert "file://" not in scenario.turns["5cards"].stdout + scenario.turns["9cards"].stdout, "cards carry no link of their own"


def test_output_contract(scenario):
    for c in scenario.calls:
        if c.code != 0:
            assert c.stdout == "" or c.args[-1] == "--json"
            continue
        if c.args[0] == "score":
            assert c.stdout.startswith("j_01 · ")
            continue
        assert re.search(r"^— rs_\d+ · \d+ · page \d+/\d+", c.state), (c.args, c.state)
        assert c.stderr == "", c.args
