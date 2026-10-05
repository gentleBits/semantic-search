#!/usr/bin/env python3
"""Wall-clock time of the results panel's steps over HTTP against a running `resumes web` (target: ≤ 100 ms each).

    .venv/bin/python scripts/bench_web.py [--url http://localhost:8765] [--runs 5]

Makes a throwaway conversation, replays a fixed sequence of mechanical steps, deletes the conversation.
No model is called: nothing here is paid.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request


def call(url: str, method: str = "GET", body: dict | None = None) -> tuple[dict, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw), (time.perf_counter() - t) * 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8765")
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    api = args.url.rstrip("/") + "/api"
    times: dict[str, list[float]] = {}
    sizes: dict[str, int] = {}

    def step(name: str, sid: str, **action) -> dict:
        out, ms = call(f"{api}/sessions/{sid}/action", "POST", action)
        times.setdefault(name, []).append(ms)
        sizes[name] = len(json.dumps(out))
        return out

    for _ in range(args.runs):
        opened, ms = call(f"{api}/sessions", "POST", {})
        times.setdefault("new conversation (3,026)", []).append(ms)
        sid = opened["session"]["id"]
        step("page 10 of all", sid, type="page", n=10)
        step("question → 165", sid, type="search", args={"topic": ["data pipelines"]})
        step("add filter → 25", sid, type="filter", args={"skill": ["elixir"]})
        step("next page", sid, type="next")
        step("sort by rate", sid, type="sort", by="rate")
        step("remove filter → 165", sid, type="drop", targets=["elixir"])
        step("two filters → 43", sid, type="filter", args={"remote": True, "rate_max": 80})
        step("undo", sid, type="undo")
        step("history jump", sid, type="use", set="rs_03")
        p = step("open a resume", sid, type="open", id="#2")
        _, ms = call(f"{api}/sessions/{sid}/people/{p['person']['id']}")
        times.setdefault("resume by id", []).append(ms)
        step("clear → 3,026", sid, type="clear")
        _, ms = call(f"{api}/sessions/{sid}")
        times.setdefault("reopen conversation", []).append(ms)
        call(f"{api}/sessions/{sid}", "DELETE")
    print(f"{'step':<28}{'n':>3}{'p50':>9}{'max':>9}{'answer':>10}")
    worst = 0.0
    for name, v in times.items():
        p50, mx = statistics.median(v), max(v)
        worst = max(worst, p50)
        print(f"{name:<28}{len(v):>3}{p50:>7.0f} ms{mx:>6.0f} ms{sizes.get(name, 0) / 1024:>7.1f} kB")
    print(f"slowest p50: {worst:.0f} ms (target ≤ 100 ms)")
    return 0 if worst <= 100 else 1


if __name__ == "__main__":
    raise SystemExit(main())
