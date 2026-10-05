from agentic_search.index.cards import CardInput, render
from agentic_search.index.embed import token_counter

count = token_counter("openai:text-embedding-3-large")


def _card(**over):
    c = CardInput(
        id="r000412", title="SENIOR DATA ENGINEER", years=9.2, rate=68, currency="EUR", location="Berlin, Germany",
        remote=True, availability="2 weeks",
        skills=[("Python", "used"), ("Spark", "led"), ("Airflow", "used"), ("AWS", "listed"), ("Scala", "listed")],
        did="built Kafka→Snowflake CDC pipeline (2B events/day); led 4-person platform team",
    )
    for k, v in over.items():
        setattr(c, k, v)
    return c


def test_render_layout():
    text = render(_card(), count)
    lines = text.split("\n")
    assert lines[0] == "r000412 · Senior Data Engineer · 9y · €68/h · Berlin (remote ok) · avail 2w"
    assert lines[1] == "skills: Spark, Python, Airflow · listed only: AWS, Scala"
    assert lines[2].startswith("did: built Kafka")
    assert len(lines) == 3 and "file://" not in text
    assert count(text) <= 110


def test_missing_fields_are_omitted():
    text = render(_card(years=None, rate=None, location=None, remote=None, availability=None, did=None, skills=[]), count)
    assert text == "r000412 · Senior Data Engineer"


def test_token_cap_trims_listed_then_used_then_did():
    skills = [(f"Skill{i}", "used") for i in range(40)] + [(f"Extra{i}", "listed") for i in range(20)]
    text = render(_card(skills=skills, did="x " * 120), count, max_tokens=110)
    assert count(text) <= 110
    assert "listed only" not in text  # listed skills were dropped first
    assert "did:" in text             # the did line survives, shortened


def test_a_card_that_cannot_fit_still_ends():
    # four used skills and a long did line over a budget too small for them: every trim step runs out
    skills = [(f"VeryLongSkillName{i}", "used") for i in range(4)]
    text = render(_card(title="Senior " + "Platform " * 20, skills=skills, did="x" * 300), count, max_tokens=20)
    assert text.startswith("r000412 · ") and count(text) > 20
