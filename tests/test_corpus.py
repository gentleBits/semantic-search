"""The built corpus: counts, registry, front matter, redaction and fixture ground truth (skipped when not built)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentic_search.ingest.markdown_dir import split_front_matter
from agentic_search.ingest.redact import EMAIL, URL, _PHONE_CANDIDATE, _looks_like_phone
from tests.testroot import TEST_ROOT

ROOT = Path(__file__).resolve().parents[1]
CORPUS = TEST_ROOT / "corpus"
FIXTURE = ROOT / "data" / "fixture"

pytestmark = pytest.mark.skipif(not (CORPUS / "stats.json").is_file(), reason="corpus not built")


@pytest.fixture(scope="module")
def docs() -> dict[str, tuple[dict, str]]:
    out = {}
    for p in sorted((CORPUS / "md").glob("*.md")):
        fm, body = split_front_matter(p.read_text(encoding="utf-8"))
        out[p.stem] = (fm, body)
    return out


@pytest.fixture(scope="module")
def registry() -> dict:
    return json.loads((CORPUS / "registry.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    return json.loads((FIXTURE / "ground_truth.json").read_text(encoding="utf-8"))


def test_counts(docs, ground_truth):
    real = [i for i, (fm, _) in docs.items() if not fm["fixture"]]
    fixture = [i for i, (fm, _) in docs.items() if fm["fixture"]]
    assert len(real) == 2647, "2,648 unique CSV texts minus the one empty row"
    assert len(fixture) == ground_truth["summary"]["total"] == 400


def test_registry_is_dense_unique_and_matches_files(docs, registry):
    nos = sorted(e["doc_no"] for e in registry["docs"])
    assert nos == list(range(1, len(nos) + 1)), "doc_no must be dense from 1 with no gaps (deleted rows keep theirs)"
    assert registry["next_doc_no"] == len(nos) + 1
    live = {f"r{e['doc_no']:06d}" for e in registry["docs"] if not e.get("deleted")}
    assert live == set(docs), "every live registry entry has exactly one file and vice versa"
    keys = [f"{e['source']}:{e['source_id']}" for e in registry["docs"]]
    assert len(keys) == len(set(keys))


def test_every_file_has_the_front_matter_the_index_needs(docs):
    required = {"id", "doc_no", "source", "source_id", "category", "hash", "words", "rate", "currency", "rate_source", "seniority_hint", "dup_group", "fixture", "converter"}
    for i, (fm, body) in docs.items():
        assert required <= set(fm), i
        assert fm["id"] == i and f"r{fm['doc_no']:06d}" == i
        assert fm["currency"] == "EUR" and isinstance(fm["rate"], int) and 15 <= fm["rate"] <= 250, i
        assert fm["rate_source"] == ("document" if fm["fixture"] else "synthetic"), i
        assert body.startswith("# "), f"{i} has no headline"
        assert "\n## " in body, f"{i} has no section"
        assert fm["words"] == len(body.split()), i


def test_no_contact_details_survive(docs):
    leaks = []
    for i, (_, body) in docs.items():
        if EMAIL.search(body) or URL.search(body):
            leaks.append((i, "email/url"))
        for m in _PHONE_CANDIDATE.finditer(body):
            if _looks_like_phone(m.group(0), body[: m.start()]):
                leaks.append((i, m.group(0)))
    assert leaks == []


def test_no_html_or_mojibake(docs):
    html = re.compile(r"</?(?:div|span|p|ul|li|br|table|td|tr|b|i|u|font)\b[^>]*>", re.I)
    moj = re.compile(r"Ã.|â€|â¢|Â")
    bad = [i for i, (_, b) in docs.items() if html.search(b) or moj.search(b)]
    assert bad == []


def test_hashes_are_unique_and_match_bodies(docs):
    from agentic_search.ingest.dedupe import content_hash

    hashes = [fm["hash"] for fm, _ in docs.values()]
    assert len(hashes) == len(set(hashes)), "exact duplicates must have been collapsed"
    for i, (fm, body) in list(docs.items())[::50]:
        assert content_hash(body) == fm["hash"], i


def test_fixture_ground_truth_lines_up(docs, ground_truth):
    by_source_id = {fm["source_id"]: (fm, body) for fm, body in docs.values() if fm["fixture"]}
    assert set(by_source_id) == set(ground_truth["docs"])
    for fid, gt in ground_truth["docs"].items():
        fm, body = by_source_id[fid]
        assert fm["rate"] == gt["rate"] and fm["seniority"] == gt["seniority"] and fm["years"] == gt["years"], fid
        if gt["elixir"] == "direct":
            assert re.search(r"\bElixir\b", body), fid
        if gt["elixir"] == "phoenix_only":
            assert re.search(r"\bPhoenix\b", body) and re.search(r"\bLiveView\b", body) and not re.search(r"\bElixir\b", body), fid
        if gt["elixir"] == "none" and not gt["negative_control"]:
            assert not re.search(r"\b(Elixir|Phoenix|LiveView)\b", body), fid
        if gt["negative_control"]:
            assert not re.search(r"\b(Elixir|LiveView)\b", body), fid
        if not gt["pipeline"]:
            assert not re.search(r"\b(pipelines?|ETL|Airflow|dbt|Spark|Flink)\b", body, re.I), fid


def test_fixture_near_duplicates_are_grouped(docs, ground_truth):
    by_source_id = {fm["source_id"]: fm for fm, _ in docs.values() if fm["fixture"]}
    group_sizes: dict[int, int] = {}
    for fm, _ in docs.values():
        if fm["dup_group"] is not None:
            group_sizes[fm["dup_group"]] = group_sizes.get(fm["dup_group"], 0) + 1
    for fid, gt in ground_truth["docs"].items():
        if gt["near_dup_of"]:
            a, b = by_source_id[fid], by_source_id[gt["near_dup_of"]]
            assert a["dup_group"] is not None and a["dup_group"] == b["dup_group"], (fid, gt["near_dup_of"])
            assert group_sizes[a["dup_group"]] == 2, f"{fid}: a pair must not absorb unrelated documents"
            assert a["doc_no"] > b["doc_no"], "the updated version was added later"
    # no fixture document is grouped with a real one
    for fm, _ in docs.values():
        if fm["fixture"] and fm["dup_group"] is not None:
            members = [f for f, _ in docs.values() if f["dup_group"] == fm["dup_group"]]
            assert all(m["fixture"] for m in members), fm["id"]


def test_stats_agree_with_files(docs):
    stats = json.loads((CORPUS / "stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == len(docs)
    assert stats["quality"] == {"no_headline": 0, "under_50_words": stats["quality"]["under_50_words"], "html_tag_leftovers": 0, "mojibake_leftovers": 0, "no_h2_section": 0}
    assert stats["quality"]["under_50_words"] <= 10
