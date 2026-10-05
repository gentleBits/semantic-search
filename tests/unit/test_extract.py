from datetime import date
from pathlib import Path

import pytest

from agentic_search.extract.deterministic import extract
from agentic_search.extract.sections import parse, parse_date_range, total_years
from agentic_search.vocab.resolve import Vocabulary

ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 9, 25)


@pytest.fixture(scope="module")
def vocab():
    return Vocabulary.load(ROOT / "schema" / "vocab.csv")


MD = """# SENIOR DATA ENGINEER

## Summary

Engineer who builds pipelines. I excel at communication and go the extra mile.

## Skills

Python, SQL, Airflow, Excel, Word, PowerPoint, Go, Phoenix (E1)

## Experience

### Senior Data Engineer
*Company Name — City, State · Jan 2020 to Current*

- Led a team of 4 building data pipelines with Airflow and dbt on Snowflake.
- Wrote Spark jobs in Scala.

### Data Engineer
*Ledgerly — Berlin, Germany · 03/2016 to 12/2019*

- Maintained ETL jobs and REST APIs; ran Kafka Connect.

### Analyst
*Company Name — City, State · 2014 to 2016*

- Reporting in Excel and SQL.

## Education

### Bachelor of Science, Computer Science
*University of Phoenix — Phoenix, AZ · 2014*

## Interests

Go hiking; Rust cars.
"""


def test_parse_structure():
    doc = parse(MD, TODAY)
    assert doc.headline == "SENIOR DATA ENGINEER"
    kinds = [(s.title, s.kind) for s in doc.sections]
    assert kinds == [("Summary", "summary"), ("Skills", "skills"), ("Experience", "experience"), ("Education", "education"), ("Interests", "skip")]
    exp = doc.experience_entries
    assert [e.title for e in exp] == ["Senior Data Engineer", "Data Engineer", "Analyst"]
    assert exp[0].company == "Company Name" and exp[0].location == "City, State" and exp[0].dates.open_ended
    assert exp[1].company == "Ledgerly" and exp[1].location == "Berlin, Germany"
    assert exp[1].dates.start == (2016, 3) and exp[1].dates.end == (2019, 12)
    assert exp[0].lines[0].startswith("Led a team")


@pytest.mark.parametrize(
    "text, start, end, open_ended",
    [
        ("Dec 2013 to Current", (2013, 12), (2026, 9), True),
        ("October 2010 to July 2015", (2010, 10), (2015, 7), False),
        ("05/2014 to 04/2016", (2014, 5), (2016, 4), False),
        ("2013 – 2017", (2013, 1), (2017, 12), False),
        ("Jun 2004 - Present", (2004, 6), (2026, 9), True),
        ("1999", (1999, 1), (1999, 12), False),
        ("Sept 2021 to Current", (2021, 9), (2026, 9), True),
        ("2019-03 to 2020-06", (2019, 3), (2020, 6), False),
    ],
)
def test_parse_date_range(text, start, end, open_ended):
    r = parse_date_range(text, TODAY)
    assert r is not None and (r.start, r.end, r.open_ended) == (start, end, open_ended)


def test_parse_date_range_rejects_garbage():
    assert parse_date_range("dates available on request", TODAY) is None
    assert parse_date_range("2019 to 2015", TODAY) is None


def test_total_years_merges_overlaps():
    doc = parse(MD, TODAY)
    # the analyst job overlaps the next one, so Jan 2014 to TODAY (Sep) merges into 12.8 years
    assert total_years(doc.experience_entries, TODAY) == 12.8
    assert total_years([], TODAY) is None


def test_deterministic_extract(vocab):
    p = extract(MD, vocab, TODAY)
    assert p.years == 12.8 and p.years_source == "computed"
    assert p.education == "bachelor"
    assert p.current_title == "Senior Data Engineer"
    terms = {t.slug: t for t in p.terms}
    assert terms["airflow"].level == "led" and terms["dbt"].level == "led"
    assert terms["etl"].level == "used" and terms["rest-apis"].level == "used" and terms["kafka-connect"].level == "used"
    assert terms["data-pipelines"].level == "led"
    assert terms["python"].level == "listed"
    # ambiguity: Excel is confirmed by context (Word/PowerPoint) in Skills and appears used in Experience
    assert terms["excel"].level == "used"
    # "Go" in a skills list with no Go-specific context (golang, goroutines, gRPC…) is rejected; "Go hiking" is in a skipped section
    assert "go" not in terms
    # "Phoenix (E1)" in Skills: no LiveView/Elixir anywhere → not the framework; Education is never scanned
    assert "phoenix-framework" not in terms and "elixir" not in terms
    assert "rust" not in terms
    assert p.ambiguous_rejected >= 2


def test_phoenix_only_document_is_elixir_via_closure(vocab):
    md = "# Backend Engineer\n\n## Skills\n\nPhoenix, LiveView, Postgres\n\n## Experience\n\n### Backend Engineer\n*Acme — Berlin · Jan 2022 to Current*\n\n- Built dashboards with the Phoenix Framework and LiveView.\n"
    p = extract(md, vocab, TODAY)
    terms = {t.slug: t for t in p.terms}
    assert terms["phoenix-framework"].level == "used"
    assert "elixir" in vocab.implied("phoenix-framework")


def test_kaggle_layout(vocab):
    md = "# Java Developer\n\n## Skills\n\n- Java, Javascript, — 6 months\n\n## Experience\n\n### Wab It Softwere Pvt. Ltd.\n\nJr. Java Developer\n\n## Education\n\n- August 2010 to May 2017 BE Electronics\n"
    p = extract(md, vocab, TODAY)
    assert p.years is None and p.education == "bachelor"
    assert {t.slug for t in p.terms} >= {"java", "javascript"}
