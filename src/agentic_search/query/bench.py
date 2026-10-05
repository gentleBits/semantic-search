"""`resumes bench`: replay a scripted session as real CLI calls and print p50/p95 per verb."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config import Config

TARGETS_MS = {"start-up": 120, "search": 150, "search --text (cold)": 600, "search --text (cached)": 600, "search --like (cached)": 600, "cards": 150, "score": 200, "sort": 200, "filter": 150, "next": 150,
              "prev": 150, "page": 150, "back": 150, "sets": 150, "show": 150,
              # removing a filter costs what paging costs: nothing is searched again
              "drop": 100, "drop (seen before)": 100, "clear": 100, "filters": 150, "cards (refused)": 150, "page 10 of all CVs": 150,
              "drop beside a --text filter": 100}


def cli() -> list[str]:
    exe = Path(sys.executable).parent / "resumes"
    return [str(exe)] if exe.is_file() else [sys.executable, "-m", "agentic_search.cli"]


def timed(args: list[str], env: dict, cwd: Path, stdin: str | None = None) -> tuple[subprocess.CompletedProcess, float]:
    t0 = time.perf_counter()
    r = subprocess.run(cli() + args, capture_output=True, text=True, env=env, cwd=cwd, input=stdin)
    return r, (time.perf_counter() - t0) * 1000


def generated_scores(card_ids: list[str]) -> dict:
    scores = []
    for cid in card_ids:
        h = int(hashlib.sha256(cid.encode()).hexdigest()[:8], 16) % 101
        scores.append({"id": cid, "score": h, "note": f"generated {h}"})
    return {"criterion": "bench: generated from a hash of each id", "judge": "resumes bench", "scores": scores}


def percentile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def run(cfg: Config, *, runs: int = 5, as_json: bool = False, log=print) -> int:
    root = cfg.root
    times: dict[str, list[float]] = {}
    text_query = "nightly batch jobs that moved data between systems"
    have_key = bool(os.environ.get("OPENAI_API_KEY"))
    for i in range(runs):
        sid = f"bench-{secrets.token_hex(3)}"
        env = {**os.environ, "RESUMES_SESSION": sid}
        sdir = cfg.query.sessions / sid
        try:
            r, ms = timed(["--help"], env, root)
            times.setdefault("start-up", []).append(ms)
            steps = [
                ("page 10 of all CVs", ["page", "10"]),
                ("search", ["search", "--topic", "data pipelines"]),
                ("cards (refused)", ["cards"]),                               # more than max_cards people: exit 1 by design
                ("filter", ["filter", "--skill", "elixir"]),
                ("cards", ["cards"]),
            ]
            for name, args in steps:
                r, ms = timed(args, env, root)
                if r.returncode != (1 if name == "cards (refused)" else 0):
                    log(f"{name} failed: {r.stderr.strip()}")
                    return 1
                times.setdefault(name, []).append(ms)
            body = r.stdout.split("\ncards:\n", 1)[1] if "\ncards:\n" in r.stdout else ""
            ids = [line.split(" · ", 1)[0] for line in body.splitlines() if line[:1] == "r" and " · " in line and line.split(" · ", 1)[0][1:].isdigit()]
            (sdir / "scores.json").write_text(json.dumps(generated_scores(ids)), encoding="utf-8")
            rest = [
                ("score", ["score", "--from", "scores.json"]),
                ("sort", ["sort", "--by", "judgment,rate"]),
                ("next", ["next"]), ("next", ["next"]), ("next", ["next"]),
                ("prev", ["prev"]), ("page", ["page", "1"]),
                ("drop", ["drop", "f2"]),
                ("filter", ["filter", "--remote", "--rate-max", "80"]),
                ("filters", ["filters"]),
                ("filter", ["filter", "--skill", "elixir"]),
                ("drop (seen before)", ["drop", "elixir"]),                   # a combination already made: cached
                ("back", ["back"]), ("sets", ["sets"]), ("show", ["show", ids[0]]),
                ("clear", ["clear"]),
            ]
            if have_key:
                rest.append(("search --text (cold)", ["search", "--text", f"{text_query} #{secrets.token_hex(2)}"]))   # never cached: real API latency
                rest.append(("search --text (cached)", ["search", "--text", text_query]))
                rest.append(("filter", ["filter", "--seniority", "senior"]))
                rest.append(("drop beside a --text filter", ["drop", "seniority"]))
                rest.append(("search --like (cached)", ["search", "--like", "eval/jds/java-backend.md"]))
            for name, args in rest:
                r, ms = timed(args, env, root)
                if r.returncode != 0:
                    log(f"{name} failed: {r.stderr.strip()}")
                    return 1
                times.setdefault(name, []).append(ms)
        finally:
            shutil.rmtree(sdir, ignore_errors=True)
    rows = []
    for name, xs in times.items():
        target = TARGETS_MS.get(name)
        p50, p95 = percentile(xs, 0.5), percentile(xs, 0.95)
        rows.append({"verb": name, "n": len(xs), "p50_ms": round(p50), "p95_ms": round(p95), "max_ms": round(max(xs)), "target_ms": target,
                     "ok": (p95 <= target) if target else None})
    if as_json:
        print(json.dumps({"runs": runs, "index_version": None, "results": rows}, indent=1))
        return 0
    log(f"resumes bench · {runs} run(s) · wall-clock per CLI call (process start included) · against the time targets")
    log(f"{'verb':<28} {'n':>3} {'p50':>7} {'p95':>7} {'max':>7} {'target':>8}  ")
    for r in rows:
        status = "" if r["ok"] is None else ("ok" if r["ok"] else "OVER")
        log(f"{r['verb']:<28} {r['n']:>3} {r['p50_ms']:>5} ms {r['p95_ms']:>5} ms {r['max_ms']:>5} ms {str(r['target_ms'] or '—'):>5} ms  {status}")
    if not have_key:
        log("search --text skipped: OPENAI_API_KEY not set")
    return 0
