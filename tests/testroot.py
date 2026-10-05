"""The tests' own index in .test/: the app's sources plus the synthetic fixture (data/fixture), whose people the
scenarios are written about. Built once, and again whenever its inputs change."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get("RESUMES_TEST_ROOT", ROOT / ".test"))
# time limits are this Mac's speed; a slower machine (CI) sets RESUMES_TIME_FACTOR=5
TIME_FACTOR = float(os.environ.get("RESUMES_TIME_FACTOR") or 1)
FIXTURE_SOURCE = '[[sources]]\nid = "fixture"\nloader = "markdown_dir"\npath = "data/fixture/md"\n\n'


def config_text() -> str:
    text = (ROOT / "resumes.toml").read_text(encoding="utf-8")
    for anchor in ('[[sources]]\nid = "inbox"', 'cache = "index/cache"', "[web]"):
        if anchor not in text:
            raise RuntimeError(f"tests/testroot.py: resumes.toml no longer has {anchor!r}; update config_text()")
    inbox = text.index('[[sources]]\nid = "inbox"')
    text = text[:inbox] + FIXTURE_SOURCE + text[inbox:]
    text = text.replace('cache = "index/cache"', f'cache = "{ROOT / "index" / "cache"}"', 1)
    return text.replace("[web]", f'[web]\napp_dir = "{ROOT / "web-ts"}"', 1)


def _stamp() -> str:
    raw = tomllib.loads(config_text())
    h = hashlib.sha256(json.dumps({k: raw.get(k) for k in ("corpus", "sources", "index")}, sort_keys=True).encode())
    inputs = [ROOT / "schema" / "vocab.csv", *sorted((ROOT / "data" / "fixture" / "md").glob("*.md"))]
    inputs += sorted(p for d in ("ingest", "index", "extract", "vocab") for p in (ROOT / "src" / "agentic_search" / d).glob("*.py"))
    inputs += [ROOT / "src" / "agentic_search" / "config.py"]
    for p in inputs:
        h.update(p.name.encode() + p.read_bytes())
    for s in ("resume_dataset_livecareer.csv", "UpdatedResumeDataSet.csv"):
        p = ROOT / "data" / s
        h.update(f"{s}:{p.stat().st_size if p.exists() else -1}".encode())
    return h.hexdigest()


def ensure(log=print) -> str | None:
    """Builds the test index if it is missing or stale. → None when it is ready, else why not."""
    from agentic_search import config as config_mod
    from agentic_search.errors import ResumesError
    from agentic_search.index.store import current_path

    TEST_ROOT.mkdir(exist_ok=True)
    for name in ("data", "schema", "eval"):
        link = TEST_ROOT / name
        if not link.exists():
            link.symlink_to(ROOT / name)
    toml = TEST_ROOT / "resumes.toml"
    if not toml.is_file() or toml.read_text(encoding="utf-8") != config_text():
        toml.write_text(config_text(), encoding="utf-8")
    stamp_file = TEST_ROOT / ".stamp"
    stamp = _stamp()
    cfg = config_mod.load(TEST_ROOT)
    if current_path(cfg.index.out) is not None and stamp_file.is_file() and stamp_file.read_text() == stamp:
        return None
    from agentic_search.index.build import build
    from agentic_search.ingest.corpus import build as build_corpus

    log("tests · building the test index in .test/ (the public data + the fixture, from the cache) …")
    try:
        build_corpus(cfg)
        build(cfg, log=lambda *a: None)
    except (ResumesError, FileNotFoundError) as e:
        return str(e)
    stamp_file.write_text(stamp)
    return None
