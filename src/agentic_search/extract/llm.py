"""LLM extraction: structured fields with verbatim evidence, cached per content hash.

Schema: headline, current_title, seniority, years_experience, education, location, remote,
availability, skills[{name, level, evidence}], topics[{slug, level, evidence}], did{text, evidence},
summary. Every `evidence` must be a verbatim substring of the document (checked in code): a
skill that fails is downgraded to `listed`; a `did` that fails is dropped (the build then uses
the first bullet of the most recent job). Precedence: deterministic values win for years and
education; the higher level wins for a term.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..errors import ResumesError
from ..vocab.resolve import Vocabulary
from .deterministic import LEVEL_RANK, DocTerm

PROMPT_VERSION = "v1"
SENIORITY = ["intern", "junior", "mid", "senior", "lead", "principal", "manager", "director", "executive", "unknown"]
EDUCATION = ["none", "high_school", "associate", "bachelor", "master", "phd", "unknown"]

SYSTEM = (
    "You extract structured facts from one resume (CV) in Markdown. Output JSON matching the schema exactly. "
    "Rules: every `evidence` field must be a VERBATIM substring copied from the resume (same words, same order; "
    "punctuation may be trimmed at the ends) — never paraphrase evidence. 'Company Name' and 'City, State' are "
    "redaction placeholders, not real values: never use them as employer, location or evidence. "
    "Skills are technologies, tools, methods and certifications; level `listed` = only appears in a skills list or summary, "
    "`used` = applied in a job or project, `led` = the person led, owned or architected work with it. "
    "Topics are areas of work the person actually worked on; use ONLY slugs from the provided list. "
    "`did` is the single most impressive concrete accomplishment, ≤ 110 characters, written as a short phrase "
    "(you may condense, but `did.evidence` must be the verbatim passage it comes from). "
    "`summary` is 2–3 dense sentences for a search index: role, domain, scale, standout strengths. "
    "years_experience: total professional years, estimated from dates or stated; null if impossible. "
    "Location, remote and availability only if stated; otherwise null."
)


def schema(topic_slugs: list[str]) -> dict:
    ev = {"type": "string"}
    return {
        "name": "resume_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "headline": {"type": "string"},
                "current_title": {"type": "string"},
                "seniority": {"type": "string", "enum": SENIORITY},
                "years_experience": {"type": ["number", "null"]},
                "education": {"type": "string", "enum": EDUCATION},
                "location": {"type": ["string", "null"]},
                "remote": {"type": ["boolean", "null"]},
                "availability": {"type": ["string", "null"]},
                "skills": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"name": {"type": "string"}, "level": {"type": "string", "enum": ["listed", "used", "led"]}, "evidence": ev},
                        "required": ["name", "level", "evidence"],
                    },
                },
                "topics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"slug": {"type": "string", "enum": topic_slugs}, "level": {"type": "string", "enum": ["used", "led"]}, "evidence": ev},
                        "required": ["slug", "level", "evidence"],
                    },
                },
                "did": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {"text": {"type": "string"}, "evidence": ev},
                    "required": ["text", "evidence"],
                },
                "summary": {"type": "string"},
            },
            "required": ["headline", "current_title", "seniority", "years_experience", "education", "location", "remote",
                         "availability", "skills", "topics", "did", "summary"],
        },
    }


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def evidence_ok(evidence: str, body_norm: str) -> bool:
    e = _norm(evidence).strip(" .,;:—-–\"'")
    return len(e) >= 3 and e in body_norm


def topic_list_text(vocab: Vocabulary) -> str:
    return "\n".join(f"- {t.slug}: {t.canonical} — {t.description}" for t in vocab.terms if t.kind == "topic")


class Extractor:
    def __init__(self, model: str, vocab: Vocabulary, cache_dir: Path, embedder=None, log=print) -> None:
        self._client = None
        self.model = model
        self.vocab = vocab
        self.cache_dir = cache_dir / "extract"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self.log = log
        self.topics = [t.slug for t in vocab.terms if t.kind == "topic"]
        self.schema = schema(self.topics)
        self.topic_text = topic_list_text(vocab)
        self._supports_effort = True
        self._lock = threading.Lock()
        self.unresolved: Counter = Counter()
        self.usage = {"in": 0, "out": 0}
        self._term_vecs = None

    @property
    def client(self):
        if self._client is None:
            from ..keys import openai_client

            self._client = openai_client(f"the LLM step for resumes not in the cache ({self.model})")
        return self._client

    # ---------------------------------------------------------------- API
    def _cache_path(self, content_hash: str) -> Path:
        safe_model = re.sub(r"[^A-Za-z0-9.-]", "_", self.model)
        return self.cache_dir / f"{content_hash}-{safe_model}-{PROMPT_VERSION}.json"

    def _call(self, body: str, retry_note: str | None = None) -> tuple[dict, dict]:
        user = "Topic slugs (use only these):\n" + self.topic_text + "\n\nResume:\n\n" + body
        if retry_note:
            user += "\n\nYour previous answer was rejected: " + retry_note + " Fix it."
        kwargs = dict(
            model=self.model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": self.schema},
            max_completion_tokens=6000,
        )
        for attempt in range(5):
            try:
                if self._supports_effort:
                    try:
                        r = self.client.chat.completions.create(**kwargs, reasoning_effort="low")
                    except Exception as e:
                        if "reasoning_effort" not in str(e):
                            raise
                        self._supports_effort = False
                        r = self.client.chat.completions.create(**kwargs)
                else:
                    r = self.client.chat.completions.create(**kwargs)
                break
            except Exception as e:
                if attempt == 4:
                    raise
                time.sleep(min(30, 2 ** attempt))
        text = r.choices[0].message.content or ""
        usage = {"in": r.usage.prompt_tokens, "out": r.usage.completion_tokens} if r.usage else {"in": 0, "out": 0}
        return json.loads(text), usage

    # ---------------------------------------------------------------- resolution
    def _resolve_skill(self, name: str) -> str | None:
        t = self.vocab.resolve(name)
        if t:
            return t.slug
        return None

    def resolve_by_embedding(self, names: list[str], threshold: float = 0.85) -> dict[str, str]:
        """Nearest canonical term by embedding for names the alias table did not resolve."""
        if not names or self.embedder is None:
            return {}
        import numpy as np

        if self._term_vecs is None:
            skills = [t for t in self.vocab.terms if t.kind == "skill"]
            vecs = self.embedder.embed([f"{t.canonical}: {t.description}" for t in skills])
            self._term_vecs = (skills, np.asarray(vecs, dtype=np.float32))
        skills, T = self._term_vecs
        Q = np.asarray(self.embedder.embed(names), dtype=np.float32)
        S = Q @ T.T
        out: dict[str, str] = {}
        for i, name in enumerate(names):
            j = int(S[i].argmax())
            if S[i, j] >= threshold:
                out[name] = skills[j].slug
        return out

    # ---------------------------------------------------------------- per document
    def extract_one(self, content_hash: str, body: str) -> dict:
        path = self._cache_path(content_hash)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        raw, usage = self._call(body)
        with self._lock:
            self.usage["in"] += usage["in"]
            self.usage["out"] += usage["out"]
        record = {"raw": raw, "usage": usage, "model": self.model, "prompt_version": PROMPT_VERSION,
                  "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return record


def merge(record: dict, body: str, vocab: Vocabulary, extractor: Extractor | None = None) -> tuple[dict, list[DocTerm], list[str], dict]:
    """Validate evidence, resolve names → (profile fields, doc terms, unresolved skill names, stats)."""
    raw = record["raw"]
    body_norm = _norm(body)
    stats = {"skills": 0, "skills_evidence_failed": 0, "skills_unresolved": 0, "topics": 0, "topics_evidence_failed": 0, "did_failed": 0}
    terms: dict[str, DocTerm] = {}
    unresolved: list[str] = []
    for s in raw.get("skills", []):
        name, level, ev = s["name"].strip(), s["level"], s["evidence"]
        if not name:
            continue
        stats["skills"] += 1
        slug = vocab.resolve(name)
        slug = slug.slug if slug else None
        if slug is None:
            unresolved.append(name)
            stats["skills_unresolved"] += 1
            continue
        if level != "listed" and not evidence_ok(ev, body_norm):
            stats["skills_evidence_failed"] += 1
            level = "listed"
        cur = terms.get(slug)
        if cur is None or LEVEL_RANK[level] > LEVEL_RANK[cur.level]:
            terms[slug] = DocTerm(slug, level, "llm", ev[:200], "llm")
    for t in raw.get("topics", []):
        slug, level, ev = t["slug"], t["level"], t["evidence"]
        if slug not in vocab.by_slug:
            continue
        stats["topics"] += 1
        if not evidence_ok(ev, body_norm):
            stats["topics_evidence_failed"] += 1
            continue  # a topic without a real quote is not asserted
        cur = terms.get(slug)
        if cur is None or LEVEL_RANK[level] > LEVEL_RANK[cur.level]:
            terms[slug] = DocTerm(slug, level, "llm", ev[:200], "llm")
    did = raw.get("did")
    did_text = None
    if did and did.get("text"):
        if evidence_ok(did.get("evidence", ""), body_norm):
            did_text = did["text"].strip()[:110]
        else:
            stats["did_failed"] += 1
    fields = {
        "headline": raw.get("headline"),
        "current_title": (raw.get("current_title") or "").strip() or None,
        "seniority": raw.get("seniority") if raw.get("seniority") not in (None, "unknown") else None,
        "years_experience": raw.get("years_experience"),
        "education": raw.get("education") if raw.get("education") not in (None, "unknown", "none") else None,
        "location": raw.get("location"),
        "remote": raw.get("remote"),
        "availability": raw.get("availability"),
        "did": did_text,
        "summary": (raw.get("summary") or "").strip() or None,
        "model": record.get("model"),
        "extracted_at": record.get("extracted_at"),
    }
    return fields, list(terms.values()), unresolved, stats


def run_llm_extraction(docs: list, vocab: Vocabulary, cfg: Config, extractor_spec: str, log=print, seen: dict | None = None) -> dict:
    """Fill `d.llm` and merge terms into `d.terms` for every BuildDoc. Cached; concurrent; never fatal per document."""
    kind, _, model = extractor_spec.partition(":")
    if kind != "openai":
        raise ValueError(f"extractor {extractor_spec!r}: only openai:<model> or none is implemented")
    icfg = cfg.index
    from ..index.embed import make_embedder

    embedder = make_embedder(icfg.embedder, icfg.dim, icfg.cache)
    ex = Extractor(model, vocab, icfg.cache, embedder=embedder, log=log)
    cached = sum(1 for d in docs if ex._cache_path(d.fm["hash"]).is_file())
    log(f"  llm extraction: {len(docs)} docs, {cached} cached, {len(docs) - cached} to call ({model}, {icfg.extract_concurrency} workers)")
    if cached < len(docs) and not os.environ.get("OPENAI_API_KEY"):
        raise ResumesError("KEY_MISSING", f"the LLM step for {len(docs) - cached} resumes not in the cache needs OPENAI_API_KEY "
                                          "in the environment (or build without it: `resumes index build --extractor none`)")
    t0 = time.time()
    failures: list[str] = []
    records: dict[int, dict] = {}

    def work(d) -> tuple[int, dict | None, str | None]:
        try:
            return d.doc_no, ex.extract_one(d.fm["hash"], d.body), None
        except Exception as e:  # keep the deterministic profile for this document
            return d.doc_no, None, f"{d.id}: {type(e).__name__}: {str(e)[:120]}"

    with ThreadPoolExecutor(max_workers=icfg.extract_concurrency) as pool:
        futs = [pool.submit(work, d) for d in docs]
        done = 0
        for f in as_completed(futs):
            doc_no, rec, err = f.result()
            done += 1
            if err:
                failures.append(err)
            elif rec:
                records[doc_no] = rec
            if done % 500 == 0:
                log(f"    {done}/{len(docs)} ({time.time() - t0:.0f}s)")

    # merge, then resolve leftover skill names by embedding in one batch
    agg = Counter()
    pending: dict[int, list[str]] = {}
    for d in docs:
        rec = records.get(d.doc_no)
        if not rec:
            continue
        fields, terms, unresolved, st = merge(rec, d.body, vocab)
        agg.update(st)
        d.llm = fields
        for t in terms:
            cur = d.terms.get(t.slug)
            if cur is None:
                d.terms[t.slug] = t
            elif LEVEL_RANK[t.level] > LEVEL_RANK[cur.level]:
                # keep the deterministic provenance (it decides membership); the LLM raises the level and supplies the quote
                d.terms[t.slug] = DocTerm(t.slug, t.level, cur.via if cur.via == "llm" else f"{cur.via}+llm", t.evidence, t.section)
        if unresolved:
            pending[d.doc_no] = unresolved
    names = sorted({n for lst in pending.values() for n in lst}, key=str.lower)
    by_emb = ex.resolve_by_embedding(names) if names else {}
    resolved_by_emb = 0
    for d in docs:
        for name in pending.get(d.doc_no, []):
            slug = by_emb.get(name)
            if slug:
                resolved_by_emb += 1
                if slug not in d.terms:
                    d.terms[slug] = DocTerm(slug, "listed", "llm-embed", name, "llm")
            else:
                ex.unresolved[name] += 1
    queue = icfg.cache / "unresolved_terms.json"
    queue.write_text(json.dumps(ex.unresolved.most_common(), ensure_ascii=False, indent=0), encoding="utf-8")
    stats = {
        "enabled": True, "model": model, "extracted": len(records), "failed": len(failures), "cached_before": cached,
        "tokens": ex.usage, "seconds": round(time.time() - t0, 1),
        "skills_seen": agg["skills"], "skills_evidence_failed": agg["skills_evidence_failed"],
        "skills_unresolved_by_alias": agg["skills_unresolved"], "skills_resolved_by_embedding": resolved_by_emb,
        "distinct_unresolved_names": len(ex.unresolved),
        "topics_seen": agg["topics"], "topics_evidence_failed": agg["topics_evidence_failed"], "did_failed": agg["did_failed"],
    }
    if failures:
        log(f"    {len(failures)} documents failed extraction (kept deterministic), e.g. {failures[:3]}")
    log(f"  llm extraction done: {json.dumps(stats)}")
    if seen is not None:
        seen["keys"] |= embedder.seen
        seen["files"] |= {str(p) for d in docs if (p := ex._cache_path(d.fm["hash"])).is_file()}
    return stats
