from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import Config
from . import personas as personas_mod


def _fixture_dir(cfg: Config) -> Path:
    return cfg.root / "data" / "fixture"


def run(args: argparse.Namespace, cfg: Config) -> int:
    fdir = _fixture_dir(cfg)
    if args.sub == "personas":
        ps = personas_mod.build_personas(cfg.fixture_seed, cfg.fixture_count)
        summary = personas_mod.write(ps, fdir)
        print(json.dumps(summary, indent=1))
        return 0

    if args.sub == "generate":
        from .generate import Generator

        ps = personas_mod.read(fdir)
        gen = Generator(args.model or cfg.fixture_model, fdir / "md", fdir / "generation_log.jsonl")
        print(json.dumps(gen.run(ps, concurrency=args.concurrency, limit=args.limit), indent=1))
        return 0

    if args.sub == "validate":
        from ..ingest.markdown_dir import split_front_matter
        from .validate import check

        ps = {p["id"]: p for p in personas_mod.read(fdir)}
        missing, bad, ok = [], {}, 0
        for pid, p in ps.items():
            path = fdir / "md" / f"{pid}.md"
            if not path.is_file():
                missing.append(pid)
                continue
            _, body = split_front_matter(path.read_text(encoding="utf-8"))
            v = check(body, p)
            if v:
                bad[pid] = v
            else:
                ok += 1
        print(json.dumps({"ok": ok, "missing": len(missing), "invalid": len(bad)}, indent=1))
        for pid, v in list(bad.items())[:20]:
            print(pid, v)
        if missing:
            print("missing:", missing[:20], "…" if len(missing) > 20 else "")
        return 0 if not bad else 1

    raise SystemExit(f"unknown fixture subcommand {args.sub}")
