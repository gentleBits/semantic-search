import re

from agentic_search.fixture import personas as pm
from agentic_search.fixture.generate import build_prompt, front_matter
from agentic_search.fixture.validate import check, sections

GOOD = """# Senior Data Engineer

## Summary
Builds data things. Reliable.

## Skills
Python, SQL, Airflow, Spark, Docker, Kubernetes

## Experience
### Senior Data Engineer
*Northwind Logistics — Berlin, Germany · Jan 2020 to Current*
- Built data pipelines with Airflow and Spark that moved 2B events a day.
- Wrote Python and SQL transformations.

### Data Engineer
*Ledgerly — Berlin, Germany · Feb 2016 to Dec 2019*
- Maintained Python jobs and SQL models; ran Airflow on a shared cluster.

## Education
### BSc, Computer Science
*TU Berlin — Berlin · 2015*
"""
GOOD = GOOD + ("\n- filler bullet sentence about the work done here.\n" * 60)  # reach the word floor


def _persona(**over):
    p = {
        "id": "f0001", "title": "Senior Data Engineer", "seniority": "senior", "years": 9, "location": "Berlin, Germany",
        "companies": ["Northwind Logistics", "Ledgerly"], "quality": "average",
        "skills_used": ["Python", "SQL", "Airflow", "Spark"], "skills_listed": ["Docker", "Kubernetes"],
        "required": ["data pipelines", "Airflow"], "forbidden": [r"\bElixir\b"], "special": None,
        "extra_instructions": [], "near_dup_of": None, "name": "A B", "category": "Data Engineering",
        "rate": 90, "currency": "EUR", "remote": True, "availability": "2 weeks",
    }
    p.update(over)
    return p


def test_sections_split():
    s = sections(GOOD)
    assert set(s) >= {"_head", "summary", "skills", "experience", "education"}
    assert "Northwind" in s["experience"] and "TU Berlin" in s["education"]


def test_check_accepts_good_resume():
    assert check(GOOD, _persona()) == []


def test_check_flags_each_rule():
    p = _persona(required=["Elixir"], forbidden=[r"\bSpark\b"], skills_listed=["Python", "Docker"], skills_used=["Rust"])
    v = check(GOOD, p)
    joined = "\n".join(v)
    assert 'required phrase missing: "Elixir"' in joined
    assert "forbidden text present" in joined and "Spark" in joined
    assert 'listed-only skill "Python" must NOT appear in the Experience section' in joined
    assert 'skill "Rust" must appear in an Experience bullet' in joined


def test_check_layout_rules():
    v = check("---\nx: 1\n---\n" + GOOD.replace("## Education", "## Studies"), _persona())
    assert any("front matter" in x for x in v) and any("## Education" in x for x in v)
    v = check(GOOD[:400], _persona())
    assert any("words" in x for x in v)


def test_check_special_D_dates():
    md = re.sub(r"· \w{3} \d{4} to (?:\w{3} \d{4}|Current)", "· dates available on request", GOOD)
    p = _persona(special="D", required=["dates available on request"])
    assert check(md, p) == []
    assert any("no year" in x for x in check(GOOD, _persona(special="D", required=[])))


def test_personas_distribution_and_lint():
    ps = pm.build_personas(seed=42, count=400)
    assert len(ps) == 400
    assert pm.lint(ps) == []
    gt = pm.ground_truth(ps)["summary"]
    assert gt["data_pipelines"] >= pm.N_PIPELINE
    assert gt["elixir_and_pipelines"] == pm.N_PIPELINE_ELIXIR
    assert gt["negative_controls"] == pm.N_NEGATIVE_CONTROLS
    assert gt["near_duplicates"] == pm.N_NEAR_DUPS
    assert gt["specials"] == ["B", "D", "E", "cheap_star", "expensive_bland", "java_full", "java_partial"]
    assert all(40 <= p["rate"] <= 140 for p in ps)
    ids = [p["id"] for p in ps]
    assert ids == sorted(ids) and ids[0] == "f0001" and ids[-1] == "f0400"
    # deterministic
    assert [p["name"] for p in pm.build_personas(42, 400)] == [p["name"] for p in ps]
    assert [p["name"] for p in pm.build_personas(43, 400)] != [p["name"] for p in ps]


def test_prompt_and_front_matter():
    p = _persona()
    text = build_prompt(p)
    assert "MUST include verbatim" in text and '"data pipelines"' in text and "Northwind Logistics" in text
    assert "Earlier version" in build_prompt(p, "# old\n")
    fm = front_matter(p)
    assert fm.startswith("---\nfixture: true\n") and "rate: 90" in fm and fm.endswith("---\n")
