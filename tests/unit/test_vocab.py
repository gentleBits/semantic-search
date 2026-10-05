from pathlib import Path

import pytest

from agentic_search.vocab.resolve import Vocabulary, tokenize

ROOT = Path(__file__).resolve().parents[2]
VOCAB = ROOT / "schema" / "vocab.csv"


@pytest.fixture(scope="module")
def vocab() -> Vocabulary:
    return Vocabulary.load(VOCAB)


def test_tokenize_keeps_symbols_inside_tokens():
    assert tokenize("C, C++ and C# with Node.js, .NET and scikit-learn; CI/CD!") == [
        "c", "c++", "and", "c#", "with", "node.js", ".net", "and", "scikit-learn", "ci/cd"
    ]


def test_sizes(vocab):
    skills = [t for t in vocab.terms if t.kind == "skill"]
    topics = [t for t in vocab.terms if t.kind == "topic"]
    assert len(skills) >= 250 and len(topics) >= 60


@pytest.mark.parametrize(
    "text, slug",
    [
        ("data pipelines", "data-pipelines"), ("Data-Pipelines", "data-pipelines"), ("ETL", "etl"), ("elt", "etl"),
        ("elixir", "elixir"), ("Phoenix Framework", "phoenix-framework"), ("LiveView", "phoenix-framework"),
        ("REST APIs", "rest-apis"), ("web services", "rest-apis"), ("RESTful", "rest-apis"),
        ("k8s", "kubernetes"), ("Kubernetes", "kubernetes"), ("Golang", "go"), ("React.js", "react"),
        ("MS Excel", "excel"), ("Microsoft Excel", "excel"), ("pivot tables", "excel"), ("Frobnicator", None),
    ],
)
def test_resolve(vocab, text, slug):
    t = vocab.resolve(text)
    assert (t.slug if t else None) == slug


def test_closure(vocab):
    assert "elixir" in vocab.implied("phoenix-framework")
    assert "data-pipelines" in vocab.implied("airflow") and "data-pipelines" in vocab.implied("dbt")
    assert "elixir" in vocab.implied("ecto")
    assert set(vocab.implying("data-pipelines")) >= {"airflow", "dbt", "spark", "kafka-connect", "flink", "dagster", "fivetran", "etl", "ssis"}
    assert "phoenix-framework" in vocab.implying("elixir")
    assert "java" in vocab.implied("spring")
    assert "java" not in vocab.implied("nextjs") and "react" in vocab.implied("nextjs")


def test_find_longest_match_and_symbols(vocab):
    toks = tokenize("Built REST APIs in C++ and C#; ran Spring Boot on Kubernetes (k8s) with Node.js")
    hits = {h.slug: h for h in vocab.find(toks)}
    assert hits["rest-apis"].form == "REST APIs" and hits["rest-apis"].end - hits["rest-apis"].start == 2
    assert "cpp" in hits and "csharp" in hits and "c" not in hits
    assert hits["spring"].form == "Spring Boot" and not hits["spring"].ambiguous
    assert "kubernetes" in hits and "nodejs" in hits


def test_ambiguity_rules(vocab):
    # "University of Phoenix" → Phoenix hit is ambiguous and has no context → not confirmed
    toks = tokenize("Bachelor of Science, University of Phoenix, 2016")
    hits = [h for h in vocab.find(toks) if h.slug == "phoenix-framework"]
    assert hits and hits[0].ambiguous
    assert not vocab.confirm_ambiguous(hits[0], set(toks), set())
    # "Phoenix ... LiveView" in the same document → confirmed via the non-ambiguous form LiveView
    assert vocab.confirm_ambiguous(hits[0], set(toks), {"liveview"})
    # "Phoenix" with "elixir" in the same section → confirmed via context
    assert vocab.confirm_ambiguous(hits[0], set(tokenize("Phoenix app in Elixir")), set())
    # "excel at customer service" → ambiguous, no context
    toks = tokenize("I excel at customer service and go the extra mile; I speak Spanish")
    hits = {h.slug: h for h in vocab.find(toks)}
    assert hits["excel"].ambiguous and not vocab.confirm_ambiguous(hits["excel"], set(toks), set())
    assert hits["go"].ambiguous and not vocab.confirm_ambiguous(hits["go"], set(toks), set())
    # "Excel, Word, PowerPoint" → context from siblings
    toks = tokenize("Microsoft Office: Excel, Word, PowerPoint, Outlook")
    hits = {h.slug: h for h in vocab.find(toks)}
    assert vocab.confirm_ambiguous(hits["excel"], set(toks), set())
    assert vocab.confirm_ambiguous(hits["word"], set(toks), set())


def test_no_form_is_shared(vocab):
    # Vocabulary() raises on a shared surface form; loading succeeded, so:
    assert len(vocab.by_form) >= 1200
