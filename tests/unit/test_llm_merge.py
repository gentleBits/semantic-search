from pathlib import Path

import pytest

from agentic_search.extract.llm import evidence_ok, merge, schema
from agentic_search.index.postings import _fts_words, _phrase_regex
from agentic_search.vocab.resolve import Vocabulary

ROOT = Path(__file__).resolve().parents[2]

BODY = """# Senior Data Engineer

## Skills

Python, SQL, Airflow, Kubernetes

## Experience

### Senior Data Engineer
*Acme — Berlin · Jan 2020 to Current*

- Led the migration of nightly batch jobs to Airflow, cutting run time by 40%.
- Wrote Python services deployed on Kubernetes.
"""


@pytest.fixture(scope="module")
def vocab():
    return Vocabulary.load(ROOT / "schema" / "vocab.csv")


def _record(**raw):
    base = {
        "headline": "Senior Data Engineer", "current_title": "Senior Data Engineer", "seniority": "senior",
        "years_experience": 6, "education": "bachelor", "location": "Berlin, Germany", "remote": None, "availability": None,
        "skills": [], "topics": [], "did": None, "summary": "Builds pipelines.",
    }
    base.update(raw)
    return {"raw": base, "model": "test-model", "extracted_at": "2026-09-25T00:00:00+00:00"}


def test_evidence_ok_is_whitespace_and_case_tolerant():
    norm = " ".join(BODY.split()).lower()
    assert evidence_ok("cutting run time by 40%", norm)
    assert evidence_ok("  Led the migration of\nnightly batch jobs", norm)
    assert not evidence_ok("cut run time by forty percent", norm)
    assert not evidence_ok("a", norm)


def test_merge_levels_and_evidence(vocab):
    rec = _record(
        skills=[
            {"name": "Airflow", "level": "led", "evidence": "Led the migration of nightly batch jobs to Airflow"},
            {"name": "Kubernetes", "level": "used", "evidence": "deployed on Kubernetes in the cloud"},   # not verbatim → listed
            {"name": "k8s", "level": "used", "evidence": "deployed on Kubernetes"},                       # alias → same term, keeps best level
            {"name": "Frobnicator 3000", "level": "used", "evidence": "x"},                                # unresolvable
        ],
        topics=[
            {"slug": "data-pipelines", "level": "led", "evidence": "nightly batch jobs to Airflow"},
            {"slug": "payments", "level": "used", "evidence": "processed payments"},                        # no such quote → dropped
        ],
        did={"text": "Cut nightly batch run time 40% by moving to Airflow", "evidence": "cutting run time by 40%"},
    )
    fields, terms, unresolved, st = merge(rec, BODY, vocab)
    t = {x.slug: x for x in terms}
    assert t["airflow"].level == "led" and t["airflow"].via == "llm"
    assert t["kubernetes"].level == "used"          # the alias entry with verbatim evidence wins
    assert "data-pipelines" in t and "payments" not in t
    assert unresolved == ["Frobnicator 3000"]
    assert st["skills_evidence_failed"] == 1 and st["topics_evidence_failed"] == 1 and st["did_failed"] == 0
    assert fields["did"].startswith("Cut nightly") and fields["seniority"] == "senior" and fields["education"] == "bachelor"


def test_merge_drops_did_without_evidence(vocab):
    rec = _record(did={"text": "Saved the company", "evidence": "saved the company millions"}, seniority="unknown", education="unknown")
    fields, terms, unresolved, st = merge(rec, BODY, vocab)
    assert fields["did"] is None and st["did_failed"] == 1
    assert fields["seniority"] is None and fields["education"] is None


def test_schema_is_strict(vocab):
    s = schema([t.slug for t in vocab.terms if t.kind == "topic"])
    assert s["strict"] is True and s["schema"]["additionalProperties"] is False
    assert set(s["schema"]["required"]) == set(s["schema"]["properties"])


def test_phrase_regex():
    r = _phrase_regex(_fts_words("data pipelines"))
    assert r.search("built data pipelines with Airflow") and r.search("a data-pipeline for events") and r.search("Data Pipeline design")
    assert not r.search("data warehouse and deployment pipeline")   # not adjacent
    r2 = _phrase_regex(_fts_words("web services"))
    assert r2.search("SOAP web service integration") and not r2.search("web design and customer services")
