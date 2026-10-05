"""The golden scenario over `resumes mcp` on stdio, same shape as the CLI replay (skipped without the index or `mcp`)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

import pytest
from pyroaring import BitMap

from agentic_search import config as config_mod
from agentic_search.index.store import current_path, open_current
from tests.testroot import TEST_ROOT

mcp = pytest.importorskip("mcp")
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CFG = config_mod.load(TEST_ROOT)
pytestmark = pytest.mark.skipif(current_path(CFG.index.out) is None, reason="index not built")
EXPECTED_TOOLS = {"search", "cards", "score", "sort", "filter", "drop", "clear", "filters", "next", "prev", "page", "top", "back", "use", "sets",
                  "show", "vocab", "session_new"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def call(session: ClientSession, name: str, **args) -> str:
    r = await session.call_tool(name, args)
    assert not r.is_error, f"{name} {args}: {r.content}"
    return "".join(c.text for c in r.content if getattr(c, "type", "") == "text")


async def call_error(session: ClientSession, name: str, **args) -> str:
    r = await session.call_tool(name, args)
    assert r.is_error, f"{name} {args} should have failed"
    return "".join(c.text for c in r.content if getattr(c, "type", "") == "text")


def link_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"^\d+\. ", ln)]


def card_ids(text: str) -> list[str]:
    """Ids of the cards to judge: the block after `cards:` (anchors come before it)."""
    body = text.split("\ncards:\n", 1)[1] if "\ncards:\n" in text else ""
    return [ln.split(" · ", 1)[0] for ln in body.splitlines() if re.match(r"^r\d{6} · ", ln)]


@pytest.mark.anyio
async def test_golden_scenario_over_mcp():
    sid = f"mcp-test-{secrets.token_hex(3)}"
    sdir = CFG.query.sessions / sid
    params = StdioServerParameters(command=sys.executable, args=["-m", "agentic_search.cli", "mcp", "--session", sid], cwd=str(TEST_ROOT), env=dict(os.environ))
    con = open_current(CFG.index.out)
    elixir = BitMap.deserialize(bytes(con.execute("SELECT strong_bm FROM postings p JOIN terms t USING (term_id) WHERE t.slug = 'elixir'").fetchone()[0]))
    fixture = dict(con.execute("SELECT source_id, doc_no FROM docs WHERE source = 'fixture'").fetchall())
    con.close()
    gt = json.loads((ROOT / "data" / "fixture" / "ground_truth.json").read_text(encoding="utf-8"))["docs"]
    truth_pipes = {fixture[s] for s, g in gt.items() if g["pipeline"]}
    truth_both = {fixture[s] for s, g in gt.items() if g["pipeline"] and g["elixir"] != "none"}

    def rs(n: str) -> dict:
        return json.loads((sdir / f"{n}.json").read_text(encoding="utf-8"))

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                assert init.server_info.name == "resumes"
                assert {t.name for t in (await s.list_tools()).tools} == EXPECTED_TOOLS
                # turn 1: count + facets, no list
                t1 = await call(s, "search", topic=["data pipelines"], said="who worked on data pipelines?")
                assert t1.startswith("rs_01 · ") and "cards:" not in t1 and not link_lines(t1)
                r1 = rs("rs_01")
                fx = set(r1["order"]) & set(fixture.values())
                assert 0.85 * len(truth_pipes) <= len(fx) <= 1.15 * len(truth_pipes) and r1["filters"] == ["f1"]
                # turn 2: browse the big set
                assert [ln.split(".")[0] for ln in link_lines(await call(s, "page", n=10))] == [str(i) for i in range(91, 101)]
                # turn 3: too many people to rank — refused, with filters that get under the limit
                refused = await call_error(s, "cards")
                assert f"TOO_MANY_TO_RANK {r1['count']} > 50" in refused and "narrow to ≤ 50 with:" in refused and "`resumes next`" in refused
                # turn 4: filter inherits the order
                flt = await call(s, "filter", skill=["elixir"])
                r2 = rs("rs_02")
                assert r2["order"] == [d for d in r1["order"] if d in elixir] and r2["filters"] == ["f1", "f2"]
                assert 0.85 * len(truth_both) <= len(set(r2["order"]) & set(fixture.values())) <= 1.15 * len(truth_both)
                assert len(link_lines(flt)) == 10 and r2["cursor"] == 10
                # turn 5: cards → inline scores → sort
                cards = await call(s, "cards")
                ids = card_ids(cards)
                assert len(ids) == r2["count"]
                scores = [{"id": i, "score": int(hashlib.sha256(i.encode()).hexdigest()[:8], 16) % 101, "note": "generated"} for i in ids]
                sc = await call(s, "score", scores=scores, criterion="talented; decent price = rate ≤ p50", judge="contract test")
                assert sc.startswith("j_01 · rs_02 · ") and (sdir / "scores" / "j_01.json").is_file()
                srt = await call(s, "sort", by="judgment,rate")
                assert len(link_lines(srt)) == 10 and all("★" in ln for ln in link_lines(srt))
                r3 = rs("rs_03")
                assert r3["judgment"] == "j_01" and r3["cursor"] == 10
                # turn 6: next ×3
                n1, n2, n3 = [await call(s, "next") for _ in range(3)]
                assert [ln.split(".")[0] for ln in link_lines(n1)] == [str(i) for i in range(11, min(20, r3["count"]) + 1)]
                assert [ln.split(".")[0] for ln in link_lines(n2)] == [str(i) for i in range(21, min(30, r3["count"]) + 1)]
                assert "end of set" in n3 and rs("rs_03")["cursor"] == r3["count"]
                # turn 7: remove the filter → everyone is back, the judged on top
                dropped = await call(s, "drop", targets=["elixir"])
                r4 = rs("rs_04")
                assert dropped.startswith(f"rs_04 · {r1['count']} people · removed f2 skill=elixir")
                assert set(r4["order"]) == set(r1["order"]) and r4["order"][: r3["count"]] == r3["order"] and r4["filters"] == ["f1"]
                assert link_lines(dropped) == link_lines(srt), "the ranked page of before"
                # turns 8–9: another filter; only the people not judged yet are printed; their scores join j_01
                await call(s, "filter", remote=True, rate_max=80)
                r5 = rs("rs_05")
                assert r5["filters"] == ["f1", "f3", "f4"] and 0 < r5["count"] <= 50
                listing = await call(s, "filters")
                assert listing.splitlines()[0].startswith("f1  topic=data-pipelines · ") and "rank  j_01" in listing
                more = await call(s, "cards")
                new_ids = card_ids(more)
                assert new_ids and not (set(new_ids) & set(ids)) and '"judgment":"j_01"' in more.splitlines()[0]
                sc2 = await call(s, "score", scores=[{"id": i, "score": 50, "note": "later"} for i in new_ids], criterion="ignored", judgment="j_01")
                assert sc2.startswith(f"j_01 · rs_05 · {len(new_ids)} scored · added to j_01: {r5['count']} of {r5['count']} judged")
                # turn 10: the first filter again is the same filter; the stars are the ones of turn 5
                again = await call(s, "filter", skill=["elixir"])
                assert rs("rs_06")["filters"] == ["f1", "f3", "f4", "f2"]
                stars = {ln.rsplit(" · ", 1)[1]: ln.split(" · ")[3] for ln in link_lines(srt) + link_lines(n1) + link_lines(n2)}
                assert link_lines(again) and all(ln.split(" · ")[3] == stars[ln.rsplit(" · ", 1)[1]] for ln in link_lines(again))
                # turn 11: clear, then browse everything
                cleared = await call(s, "clear")
                assert "all CVs (filters and ranking removed)" in cleared.splitlines()[0] and rs("rs_07")["filters"] == []
                assert (await call(s, "page", n=10)).splitlines()[-1].endswith("no filters (all CVs)")
                # the rest of the surface
                assert "← current" in await call(s, "sets")
                assert (await call(s, "back")).startswith("rs_06 · ")
                assert (await call(s, "use", set="rs_03")).startswith("rs_03 · ")
                assert (await call(s, "prev")).startswith("rs_03 · ")
                assert (await call(s, "page", n=1)).startswith("rs_03 · ")
                assert (await call(s, "top", n=3)).count("\n→") == 0 and "1–3 of" in await call(s, "top", n=3)
                assert (await call(s, "show", id=ids[0])).startswith(ids[0] + " · ")
                assert "implies: elixir" in await call(s, "vocab", term="phoenix")
                assert (await call(s, "drop", targets=["rank"])).splitlines()[0].endswith("removed the ranking")
                # errors are the CLI's
                assert "UNKNOWN_SET rs_77" in await call_error(s, "cards", set="rs_77")
                assert "SCORES_INCOMPLETE" in await call_error(s, "score", set="rs_02", scores=scores[:1], criterion="partial")
                assert "did you mean elixir" in await call_error(s, "search", skill=["elixer"])
                assert "UNKNOWN_FILTER" in await call_error(s, "drop", targets=["cobol"])
                # a fresh session restarts the numbering
                out = await call(s, "session_new")
                assert out.startswith("session ")
                new_sid = out.split(" · ")[0].removeprefix("session ")
                try:
                    assert (await call(s, "search", skill=["elixir"])).startswith("rs_01 · 56 people")
                finally:
                    shutil.rmtree(CFG.query.sessions / new_sid, ignore_errors=True)
    finally:
        shutil.rmtree(sdir, ignore_errors=True)
