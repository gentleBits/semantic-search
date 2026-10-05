from pathlib import Path

from agentic_search.index.chunk import chunk_document
from agentic_search.index.embed import token_counter

ROOT = Path(__file__).resolve().parents[2]
count = token_counter("openai:text-embedding-3-large")

MD = """# SENIOR DATA ENGINEER

## Summary

Builds pipelines.

## Skills

Python, SQL, Airflow

## Experience

### Senior Data Engineer
*Acme — Berlin, Germany · Jan 2020 to Current*

- Led a team building data pipelines.
- Wrote Spark jobs.

### Data Engineer
*Ledgerly — Berlin · 03/2016 to 12/2019*

- Maintained ETL jobs.

## Education

### BSc, Computer Science
*TU Berlin — Berlin · 2015*

## Interests

Hiking.
"""


def test_chunks_follow_sections_and_entries():
    chunks = chunk_document(7, MD, count)
    heads = [(c.section, c.header) for c in chunks]
    assert heads == [
        ("summary", "SENIOR DATA ENGINEER | Summary"),
        ("skills", "SENIOR DATA ENGINEER | Skills"),
        ("experience", "SENIOR DATA ENGINEER | Experience › Senior Data Engineer, Acme, Jan 2020 to Current"),
        ("experience", "SENIOR DATA ENGINEER | Experience › Data Engineer, Ledgerly, 03/2016 to 12/2019"),
        ("education", "SENIOR DATA ENGINEER | Education"),
    ]
    assert chunks[2].text == "Senior Data Engineer\nLed a team building data pipelines.\nWrote Spark jobs."
    assert all(c.doc_no == 7 and c.idx == i for i, c in enumerate(chunks))
    assert all(c.tokens == count(c.embed_text) for c in chunks)


def test_long_entry_is_split_with_overlap():
    bullets = "\n".join(f"- Bullet number {i} describing a fairly specific piece of work done on the platform." for i in range(60))
    md = f"# X\n\n## Experience\n\n### Engineer\n*Co — City · 2020 to 2021*\n\n{bullets}\n"
    chunks = chunk_document(1, md, count, max_tokens=200, overlap=30)
    assert len(chunks) >= 4
    assert all(c.tokens <= 200 for c in chunks)
    # overlap: the first line of chunk 2 was the last line of chunk 1
    first_lines = [c.text.split("\n")[0] for c in chunks]
    last_lines = [c.text.split("\n")[-1] for c in chunks]
    assert first_lines[1] == last_lines[0]


def test_real_corpus_chunks_within_budget():
    files = sorted((ROOT / "corpus" / "md").glob("*.md"))[:200]
    if not files:
        return
    from agentic_search.ingest.markdown_dir import split_front_matter

    total = 0
    for i, p in enumerate(files):
        _, body = split_front_matter(p.read_text(encoding="utf-8"))
        chunks = chunk_document(i, body, count, 350, 40)
        assert chunks, p.name
        assert all(c.tokens <= 350 for c in chunks), p.name
        total += len(chunks)
    assert 2 <= total / len(files) <= 12
