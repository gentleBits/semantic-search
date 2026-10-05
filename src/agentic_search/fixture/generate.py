"""Write fixture resumes with an LLM (OpenAI) from persona specs, validating each one.

Idempotent: personas that already have a file are skipped. Near-duplicate
versions are generated after their parents, from the parent's text.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .validate import check

SYSTEM = (
    "You write realistic resumes (CVs) for software professionals as Markdown. "
    "You follow the requested layout exactly and obey every MUST and MUST NOT rule literally — "
    "they are checked by a program. Output only the Markdown resume: no front matter, no code fences, no commentary."
)

LAYOUT = """Layout — exactly these headings, in this order, nothing else at the top level:

# <current job title>

## Summary
2–3 sentences. No name, no contact details.

## Skills
One comma-separated line (or a short bullet list) containing ALL of: {all_skills}

## Experience
### <job title>
*<Company> — <City, Country> · <Mon YYYY> to <Mon YYYY or Current>*
- 3–6 bullets, past tense, concrete; each bullet one sentence
(2–4 roles, most recent first, most recent ends "Current"; together they span about {years} years ending in 2026)

## Education
### <degree>, <field>
*<school> — <City> · <YYYY>*
"""

QUALITY = {
    "strong": "This person is genuinely talented: at least two bullets state a quantified, hard result at scale "
              "(throughput, latency, cost, revenue, team size) and one bullet shows ownership or leadership.",
    "average": "Solid but ordinary: competent work, one modest number at most, no grand claims.",
    "weak": "Generic: vague duty-style bullets ('responsible for', 'assisted with'), no numbers, no ownership.",
}


def build_prompt(p: dict, parent_md: str | None = None) -> str:
    all_skills = ", ".join(p["skills_used"] + p["skills_listed"])
    rules = [
        f"Person: {p['title']} ({p['seniority']}), about {p['years']} years of experience, based in {p['location']}.",
        f"Use these company names for the roles (pick from): {', '.join(p['companies'])}.",
        "MUST: each of these skills appears in at least one Experience bullet, spelled exactly: " + ", ".join(p["skills_used"]) + ".",
    ]
    if p["skills_listed"]:
        rules.append(
            "MUST: these skills appear in the Skills section and NOWHERE ELSE (not in any Experience bullet): "
            + ", ".join(p["skills_listed"]) + "."
        )
    if p["required"]:
        rules.append("MUST include verbatim (case does not matter): " + "; ".join(f'"{r}"' for r in p["required"]) + ".")
    if p["forbidden"]:
        rules.append("MUST NOT contain any of these words/phrases, in any form or spelling: " + ", ".join(p["forbidden"]) + ".")
    rules.append(QUALITY[p["quality"]])
    rules += p.get("extra_instructions", [])
    length = "250–500" if p["seniority"] == "junior" else "350–650"
    rules.append(f"Length: {length} words. Never write the person's name, email, phone number or street address.")
    text = LAYOUT.format(all_skills=all_skills, years=p["years"]) + "\nRules:\n" + "\n".join(f"- {r}" for r in rules)
    if parent_md:
        text += "\n\nEarlier version of this person's resume (keep ≥90% of its sentences identical, add one new most recent role on top, update the title/summary/skills minimally):\n\n" + parent_md
    return text


def front_matter(p: dict) -> str:
    fm = {
        "fixture": True,
        "name": p["name"],
        "title": p["title"],
        "category": p["category"],
        "seniority": p["seniority"],
        "years": p["years"],
        "rate": p["rate"],
        "currency": p["currency"],
        "location": p["location"],
        "remote": p["remote"],
        "availability": p["availability"],
    }
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n"


class Generator:
    def __init__(self, model: str, out_dir: Path, log_path: Path, log=print) -> None:
        self._client = None
        self.model = model
        self.out_dir = out_dir
        self.log_path = log_path
        self.log = log
        self._lock = threading.Lock()
        self._supports_effort = True

    @property
    def client(self):
        if self._client is None:
            from ..keys import openai_client

            self._client = openai_client(f"writing the missing fixture resumes ({self.model})")
        return self._client

    def _call(self, prompt: str) -> tuple[str, dict]:
        kwargs = dict(
            model=self.model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            max_completion_tokens=4000,
        )
        if self._supports_effort:
            try:
                r = self.client.chat.completions.create(**kwargs, reasoning_effort="low")
            except Exception as e:  # model without the parameter → retry once without it
                if "reasoning_effort" not in str(e):
                    raise
                self._supports_effort = False
                r = self.client.chat.completions.create(**kwargs)
        else:
            r = self.client.chat.completions.create(**kwargs)
        text = (r.choices[0].message.content or "").strip()
        usage = {"in": r.usage.prompt_tokens, "out": r.usage.completion_tokens} if r.usage else {}
        return text, usage

    @staticmethod
    def _clean(text: str) -> str:
        t = text.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else ""
            if t.rstrip().endswith("```"):
                t = t.rstrip()[:-3]
        return t.strip() + "\n"

    def one(self, p: dict, parent_md: str | None, attempts: int = 4) -> dict:
        record = {"id": p["id"], "model": self.model, "attempts": 0, "violations": [], "usage": {"in": 0, "out": 0}}
        prompt = build_prompt(p, parent_md)
        t0 = time.time()
        for attempt in range(1, attempts + 1):
            record["attempts"] = attempt
            text, usage = self._call(prompt)
            record["usage"]["in"] += usage.get("in", 0)
            record["usage"]["out"] += usage.get("out", 0)
            md = self._clean(text)
            violations = check(md, p)
            if not violations:
                (self.out_dir / f"{p['id']}.md").write_text(front_matter(p) + md, encoding="utf-8")
                record["ok"] = True
                record["words"] = len(md.split())
                break
            record["violations"].append(violations)
            prompt = build_prompt(p, parent_md) + (
                "\n\nYour previous attempt was rejected for these reasons; fix ALL of them:\n"
                + "\n".join(f"- {x}" for x in violations)
            )
        else:
            record["ok"] = False
        record["seconds"] = round(time.time() - t0, 1)
        record["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        status = "ok " if record["ok"] else "FAIL"
        self.log(f"  {status} {p['id']} {p['title'][:34]:34} attempts={record['attempts']} {record['seconds']}s")
        return record

    def run(self, personas: list[dict], concurrency: int = 8, limit: int | None = None) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        todo = [p for p in personas if not (self.out_dir / f"{p['id']}.md").is_file()]
        if limit:
            todo = todo[:limit]
        parents = [p for p in todo if not p["near_dup_of"]]
        children = [p for p in todo if p["near_dup_of"]]
        self.log(f"generating {len(parents)} resumes + {len(children)} near-duplicate versions with {self.model}, concurrency {concurrency}")
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(self.one, p, None) for p in parents]
            for f in as_completed(futs):
                results.append(f.result())
        for p in children:
            parent_path = self.out_dir / f"{p['near_dup_of']}.md"
            if not parent_path.is_file():
                self.log(f"  skip {p['id']}: parent {p['near_dup_of']} not generated yet")
                continue
            from ..ingest.markdown_dir import split_front_matter

            _, parent_md = split_front_matter(parent_path.read_text(encoding="utf-8"))
            results.append(self.one(p, parent_md))
        ok = sum(1 for r in results if r.get("ok"))
        tokens_in = sum(r["usage"]["in"] for r in results)
        tokens_out = sum(r["usage"]["out"] for r in results)
        return {"generated": ok, "failed": len(results) - ok, "tokens_in": tokens_in, "tokens_out": tokens_out}
