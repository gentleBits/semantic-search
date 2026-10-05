# Fixture corpus — specification

Why this exists: the two real CSVs (2,647 resumes) contain **no** Elixir developers, only a handful of data-pipeline
people and **no** hourly rates, so the reference scenario the tests replay (`tests/golden/`) cannot run on them. The fixture adds
~400 synthetic tech resumes whose ground truth is known exactly. The tests mix them with the real ones in their own
index (`.test/`, built by `tests/testroot.py`), so the scenario runs amid realistic noise; the app itself has only the
real ones.

How it is made (`resumes fixture personas` → `resumes fixture generate` → `resumes fixture validate`):

1. **Personas** (`personas.jsonl`) are produced by `agentic_search/fixture/personas.py` from a seeded RNG
   (`resumes.toml: [fixture] seed = 42, count = 400`). Every fact below is decided here, not by the LLM.
2. **Resumes** (`md/f0001.md …`) are written by an LLM (`gpt-5-mini`) from each persona, in the same markdown layout
   as the converted real resumes (`# title`, `## Summary`, `## Skills`, `## Experience` with `### role` entries and
   `*Company — City · Mon YYYY to Mon YYYY*` lines, `## Education`). Each output is checked against the persona
   (`validate.py`) — required phrases, forbidden words, skills in the right sections, dates, length — and regenerated
   with the violations listed until it passes (≤ 4 attempts). The files are committed; generation never runs in CI.
3. **Ground truth** (`ground_truth.json`) is written from the personas: topics, skills, Elixir status, rate,
   quality, near-duplicate links.

Front matter written into each fixture file: `fixture: true, name, title, category, seniority, years, rate,
currency, location, remote, availability`. The corpus build reads `rate` from it (`rate_source: document`).

## The mix (400 resumes)

| Slice | Count | What the resume must contain | What it must not contain |
|---|---|---|---|
| **Worked on data pipelines** (topic `data-pipelines`, exact) | 110 | 80 "canonical": the phrase "data pipelines" or "ETL" plus hands-on work with 1–3 of Airflow, dbt, Spark, Kafka Connect, Flink, Dagster, Fivetran. 30 "paraphrase-only": two of "ingestion jobs", "nightly batch jobs that moved data from", "change data capture", "streaming jobs", "loading data from source systems into", "scheduled data loads", "moved data between systems", "replicated tables from", "backfilled historical data" | paraphrase-only: the words pipeline, ETL, ELT, Airflow, dbt, Spark, Kafka, Flink, Dagster, Fivetran |
| … of which **know Elixir** | 25 | 17 say "Elixir" in an Experience bullet; 8 say only "Phoenix" + "LiveView" | phoenix-only: Elixir, Erlang, BEAM |
| … of which rate outliers | 6 | 3 `cheap_star`: junior/mid, rate €40–50, *strong* quality; 3 `expensive_bland`: senior, €130–140, *weak* quality | |
| **Elixir, no pipeline work** (distractors) | 30 | 26 "Elixir", 4 Phoenix+LiveView only | any pipeline word (see list) |
| **Negative controls** | 6 | 3: degree from "University of Phoenix"; 3: every job in "Phoenix, AZ" | Elixir, LiveView, Erlang, Ecto, "Phoenix Framework", pipeline words |
| **Java JD test candidates** | 10 | 4 `java_full`: Java + Spring Boot + Hibernate + REST APIs all used in Experience; 3 `java_partial`: two of the four; **B**: Java, Spring, Hibernate and "web services" (SOAP/HTTP), never "REST"; **E**: Senior SAP Consultant — Java/Spring/Hibernate appear only in Skills; **D**: Java/Spring Boot/REST, every date line reads "dates available on request", no year anywhere | partial: the two missing terms; B: REST/RESTful; all: pipeline + Elixir words |
| **Everyone else** | 234 | backend, frontend, mobile, DevOps/SRE, ML, QA, security, embedded, product roles; 1–2 topic phrases each (payments, search, authentication, observability, CI/CD, Kubernetes, e-commerce, design system, accessibility, MLOps, application security, firmware, robotics, IoT, recommendation, analytics, API platform) | pipeline words, Elixir/Phoenix words |
| **Near-duplicates** | 10 | the same person one year later: ≥ 90 % of sentences identical, one new most-recent role, rate × 1.1 | |

Every persona also fixes: seniority (junior 15 % · mid 35 % · senior 30 % · lead 13 % · principal 7 %), years of
experience by seniority (junior 1–3, mid 3–7, senior 7–12, lead 9–15, principal 12–20), an hourly rate by seniority
(junior €40–55, mid €55–80, senior €75–110, lead €90–130, principal €100–140) unless it is an outlier, a European
city (or Phoenix, AZ for controls), remote (70 %), availability (immediately / 2 weeks / 1 month / 3 months), a
quality label (strong 30 % · average 50 % · weak 20 %) that steers how concrete and quantified the bullets are,
5–8 skills of which the last two are *listed only* (Skills section, never in Experience) — so "used vs listed" has
exact ground truth too.

Pipeline forbidden words (for anyone who did not work on pipelines): pipeline(s), ETL, ELT, Airflow, dbt, Spark,
Kafka, Flink, Dagster, Fivetran, ingestion, change data capture, CDC, data warehouse, Snowflake, BigQuery,
Redshift, data load(s), backfill.

## What the golden scenario expects from this (with the real corpus mixed in)

- "who worked on data pipelines?" → the 110 fixture people plus whatever real resumes legitimately match
  (ETL/Spark mentions): a count around 110–150, with the 30 paraphrase-only people found by the semantic channel.
- "the talented ones at a decent price" → the `cheap_star`s must rank near the top, the `expensive_bland`s near the
  bottom, once the agent has judged the cards.
- "only those who know elixir" → exactly the 25 Elixir-and-pipeline people, in the same order; the 6 negative
  controls must not appear; the 8 Phoenix-only people must.
- `/next` → paging over the 25.
