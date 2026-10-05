"""Session store, saved filters and judgments, on a temp directory (no index needed)."""

from __future__ import annotations

import json
import os
import time

import duckdb
import pytest
from pyroaring import BitMap

from agentic_search.errors import ResumesError
from agentic_search.session import judgments, store
from agentic_search.session.bitmap import decode, encode


def test_bitmap_roundtrip():
    bm = BitMap([1, 5, 9000, 3000000])
    assert decode(encode(bm)) == bm


def test_set_id_parsing():
    assert store.parse_set_id("rs_03") == "rs_03"
    assert store.parse_set_id("rs_3") == "rs_03"
    assert store.parse_set_id("3") == "rs_03"
    assert store.parse_set_id("RS_12") == "rs_12"
    assert store.parse_set_id("current") is None and store.parse_set_id(None) is None
    with pytest.raises(ResumesError) as e:
        store.parse_set_id("foo")
    assert e.value.code == "UNKNOWN_SET"


def test_session_lifecycle(tmp_path, monkeypatch):
    monkeypatch.delenv(store.ENV_VAR, raising=False)
    base = tmp_path / "sessions"
    assert store.open_session(base, "v1", create=False) is None
    s = store.open_session(base, "v1", create=True)
    assert s is not None and s.exists() and (base / store.CURRENT_FILE).read_text().strip() == s.id
    # the same session comes back next time
    assert store.open_session(base, "v1", create=False).id == s.id
    # $RESUMES_SESSION overrides the file
    monkeypatch.setenv(store.ENV_VAR, "agent-abc")
    assert store.open_session(base, "v1", create=False) is None
    s2 = store.open_session(base, "v1", create=True)
    assert s2.id == "agent-abc" and s2.dir == base / "agent-abc"
    with pytest.raises(ResumesError):
        monkeypatch.setenv(store.ENV_VAR, "bad id/with slash")
        store.open_session(base, "v1", create=True)
    monkeypatch.delenv(store.ENV_VAR)
    ids = [x["id"] for x in store.list_sessions(base)]
    assert set(ids) == {s.id, "agent-abc"}


def test_sets_cursor_lineage(tmp_path):
    s = store.Session.create(tmp_path / "sid", "v1")
    assert s.current() is None
    with pytest.raises(ResumesError) as e:
        s.resolve(None)
    assert e.value.code == "NO_CURRENT_SET"
    r1 = s.new_set(parent=None, op="search", args={"topic": ["x"]}, index_version="v1", order=[5, 3, 9, 1])
    assert r1.id == "rs_01" and r1.count == 4 and r1.cursor == 0 and s.meta["current"] == "rs_01"
    r2 = s.new_set(parent="rs_01", op="filter", args={}, index_version="v1", order=[3, 1], judgment="j_01")
    assert r2.id == "rs_02" and s.set_ids() == ["rs_01", "rs_02"]
    assert s.lineage("rs_02") == ["rs_02", "rs_01"]
    # file contents: bitmap is derived from order
    d = json.loads(s.set_path("rs_01").read_text())
    assert decode(d["bitmap"]) == BitMap([5, 3, 9, 1]) and d["order"] == [5, 3, 9, 1]
    # a stale bitmap on disk is ignored on load
    d["bitmap"] = encode(BitMap([42]))
    s.set_path("rs_01").write_text(json.dumps(d))
    assert s.get("rs_01").bitmap() == BitMap([5, 3, 9, 1])
    # cursor writes are the only mutation
    s.save_cursor(r1, 10)
    assert s.get("rs_01").cursor == 4 and s.get("rs_01").order == [5, 3, 9, 1]
    assert s.get("rs_01").page_no == 1 and s.get("rs_01").pages == 1
    with pytest.raises(ResumesError) as e:
        s.get("rs_07")
    assert "UNKNOWN_SET rs_07 (have rs_01..rs_02)" in str(e.value)
    assert s.resolve("2").id == "rs_02" and s.resolve(None).id == "rs_02"
    s.set_current("rs_01")
    assert s.resolve(None).id == "rs_01"


def _session_with_set(tmp_path):
    s = store.Session.create(tmp_path / "sid", "v1")
    rs = s.new_set(parent=None, op="search", args={}, index_version="v1", order=[1, 2, 3])
    ids = {"r000001": 1, "r000002": 2, "r000003": 3}
    return s, rs, ids


def test_score_validation(tmp_path):
    s, rs, ids = _session_with_set(tmp_path)
    good = {"criterion": "talented", "judge": "test", "scores": [
        {"id": "r000001", "score": 86, "note": "a"}, {"id": "r000002", "score": 140, "note": "n" * 200}, {"id": "r000003", "score": 10}]}
    st = judgments.record(s, rs, json.dumps(good), ids)
    assert st.judgment_id == "j_01" and st.n_scored == 3 and st.n_clamped == 1 and st.n_truncated == 1 and st.top == ("r000002", 100)
    got = judgments.load(s, "j_01")
    assert got[2][0] == 100 and len(got[2][1]) == judgments.NOTE_MAX and got[3] == (10, "")
    assert (s.scores_dir / "j_01.json").is_file()
    for payload, code in [
        ({"criterion": "c", "scores": [{"id": "r000001", "score": 1}, {"id": "r000001", "score": 2}, {"id": "r000002", "score": 3}, {"id": "r000003", "score": 3}]}, "SCORES_DUPLICATE"),
        ({"criterion": "c", "scores": [{"id": "r000009", "score": 1}]}, "SCORES_UNKNOWN_ID"),
        ({"criterion": "c", "scores": [{"id": "r000001", "score": 1}]}, "SCORES_INCOMPLETE"),
        ({"scores": [{"id": "r000001", "score": 1}]}, "SCORES_INVALID"),
        ({"criterion": "c", "scores": [{"id": "r000001", "score": "high"}]}, "SCORES_INVALID"),
    ]:
        with pytest.raises(ResumesError) as e:
            judgments.record(s, rs, json.dumps(payload), ids)
        assert e.value.code == code, payload
    assert s.meta["next_judgment"] == 2, "a rejected file never consumes a judgment id"
    # partial with --allow-partial: missing → null, sorts last later
    st = judgments.record(s, rs, json.dumps({"criterion": "c", "scores": [{"id": "r000002", "score": 50}]}), ids, allow_partial=True)
    assert st.judgment_id == "j_02" and st.n_missing == 2
    assert judgments.load(s, "j_02") == {1: (None, ""), 2: (50, ""), 3: (None, "")}
    # the parquet holds both judgments, one row per doc each
    con = duckdb.connect()
    n = con.execute("SELECT judgment_id, count(*) FROM read_parquet(?) GROUP BY 1 ORDER BY 1", [str(s.judgments_path)]).fetchall()
    assert n == [("j_01", 3), ("j_02", 3)]
    assert judgments.newest_on_lineage(s, ["rs_01"]) == "j_02"
    assert judgments.newest_on_lineage(s, ["rs_09"]) is None
    assert [j["id"] for j in judgments.list_judgments(s)] == ["j_01", "j_02"]


def test_gc_keeps_active_and_recent(tmp_path):
    base = tmp_path / "sessions"
    old = store.Session.create(base / "old", "v1")
    new = store.Session.create(base / "new", "v1")
    past = time.time() - 30 * 86400
    for p in old.dir.rglob("*"):
        os.utime(p, (past, past))
    assert store.gc_sessions(base, 14, keep="new") == ["old"]
    assert new.exists() and not old.dir.exists()
    # the active session is never removed, however old
    for p in new.dir.rglob("*"):
        os.utime(p, (past, past))
    assert store.gc_sessions(base, 14, keep="new") == []


# ---------------------------------------------------------------- saved filters, saved views, merged judgments


def test_filter_files_and_view_index(tmp_path):
    from agentic_search.session import filters as fmod

    s = store.Session.create(tmp_path / "sid", "v1")
    f = fmod.save(s, fmod.Filter(id=s.next_filter_id(), kind="criteria", key='{"skill": ["elixir"]}', label="skill=elixir", args={"skill": ["elixir"]},
                                 index_version="v1", members=BitMap([1, 5, 9]), strong=BitMap([1, 5]), order=[5, 1, 9], said="only elixir"))
    g = fmod.load(s, "f1")
    assert (g.id, g.label, g.said, g.order, g.ranked) == ("f1", "skill=elixir", "only elixir", [5, 1, 9], True)
    assert g.members == BitMap([1, 5, 9]) and g.strong == BitMap([1, 5]) and g.created_at == f.created_at
    assert json.loads(s.filter_path("f1").read_text())["count"] == 3
    assert fmod.find(s, '{"skill": ["elixir"]}').id == "f1" and fmod.find(s, "other") is None
    c = fmod.save(s, fmod.Filter(id=s.next_filter_id(), kind="criteria", key="rate", label="rate-max=80", args={"rate_max": 80}, index_version="v1",
                                 members=BitMap([1, 2]), strong=BitMap([1, 2])))
    assert c.id == "f2" and not fmod.load(s, "f2").ranked, "a constraint ranks nothing"
    fz = fmod.frozen(s, label="rs_01 ∪ rs_02", order=[9, 1], index_version="v1")
    assert fz.id == "f3" and fz.kind == "frozen" and fmod.frozen(s, label="rs_01 ∪ rs_02", order=[9, 1], index_version="v1").id == "f3"
    assert s.filter_ids() == ["f1", "f2", "f3"] and s.meta["next_filter"] == 4
    with pytest.raises(ResumesError) as e:
        fmod.load(s, "f9")
    assert str(e.value) == "UNKNOWN_FILTER f9 (have f1, f2, f3)"
    # the set records its filters; an older set file has none
    rs = s.new_set(parent=None, op="search", args={}, index_version="v1", order=[5, 1], filters=["f1", "f2"])
    assert s.get(rs.id).filters == ["f1", "f2"] and s.get(rs.id).ranking_off is False
    d = json.loads(s.set_path(rs.id).read_text())
    del d["filters"], d["ranking_off"]
    s.set_path(rs.id).write_text(json.dumps(d))
    assert s.get(rs.id).filters is None
    s.save_cursor(s.get(rs.id), 2)
    assert s.get(rs.id).filters is None and s.get(rs.id).cursor == 2
    # saved views; an older session.json has no filter counter
    assert s.views() == {}
    s.remember_view("v1|f1+f2|by:f1||-", rs.id)
    assert s.views() == {"v1|f1+f2|by:f1||-": rs.id}
    meta = json.loads(s.path.read_text())
    del meta["next_filter"]
    s.path.write_text(json.dumps(meta))
    assert store.Session(s.dir).next_filter_id() == "f1"


def test_a_later_pass_is_merged_into_the_judgment(tmp_path):
    s = store.Session.create(tmp_path / "sid", "v1")
    ids = {f"r{n:06d}": n for n in range(1, 7)}
    small = s.new_set(parent=None, op="search", args={}, index_version="v1", order=[1, 2, 3])
    st = judgments.record(s, small, json.dumps({"criterion": "talented", "judge": "a", "scores": [
        {"id": "r000001", "score": 90, "note": "one"}, {"id": "r000002", "score": 40, "note": "two"}, {"id": "r000003", "score": 70, "note": "three"}]}), ids)
    assert st.judgment_id == "j_01" and not st.merged
    wide = s.new_set(parent="rs_01", op="drop", args={}, index_version="v1", order=[1, 2, 3, 4, 5])
    # incomplete: 4 and 5 are new; 1–3 need no score
    with pytest.raises(ResumesError) as e:
        judgments.record(s, wide, json.dumps({"scores": [{"id": "r000004", "score": 10}]}), ids, into="j_01")
    assert str(e.value).startswith("SCORES_INCOMPLETE 1 ids missing: r000005")
    # r000006 is neither in the set nor judged: an unknown id
    with pytest.raises(ResumesError) as e:
        judgments.record(s, wide, json.dumps({"scores": [{"id": "r000004", "score": 10}, {"id": "r000005", "score": 60}, {"id": "r000006", "score": 5}]}), ids, into="j_01")
    assert e.value.code == "SCORES_UNKNOWN_ID"
    st = judgments.record(s, wide, json.dumps({"criterion": "ignored", "judge": "b", "scores": [
        {"id": "r000004", "score": 10, "note": "four"}, {"id": "r000005", "score": 60, "note": "five"},
        {"id": "r000001", "score": 5, "note": "changed my mind"}]}), ids, into="j_01")          # already judged: the first score stands
    assert (st.judgment_id, st.merged, st.n_scored, st.n_kept, st.criterion) == ("j_01", True, 2, 1, "talented")
    assert judgments.load(s, "j_01") == {1: (90, "one"), 2: (40, "two"), 3: (70, "three"), 4: (10, "four"), 5: (60, "five")}
    assert s.meta["next_judgment"] == 2 and (s.scores_dir / "j_01+rs_02.json").is_file() and (s.scores_dir / "j_01.json").is_file()
    assert judgments.criterion_of(s, "j_01") == "talented" and judgments.criterion_of(s, "j_05") is None
    assert judgments.newest_on_lineage(s, ["rs_02"]) == "j_01"
    with pytest.raises(ResumesError) as e:
        judgments.record(s, wide, json.dumps({"scores": []}), ids, into="j_05")
    assert e.value.code == "UNKNOWN_JUDGMENT"
    # an unscored row (--allow-partial) is replaced by a later score
    other = s.new_set(parent=None, op="search", args={}, index_version="v1", order=[1, 6])
    judgments.record(s, other, json.dumps({"criterion": "lead", "scores": [{"id": "r000001", "score": 50}]}), ids, allow_partial=True)
    assert judgments.load(s, "j_02") == {1: (50, ""), 6: (None, "")}
    judgments.record(s, other, json.dumps({"scores": [{"id": "r000006", "score": 77, "note": "six"}]}), ids, into="j_02")
    assert judgments.load(s, "j_02") == {1: (50, ""), 6: (77, "six")}
    assert judgments.same_criterion(" Talented;  decent price ", "talented; decent price") and not judgments.same_criterion("", "")
