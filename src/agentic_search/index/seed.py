"""The model's saved outputs for the public data, packed into one file, so a new install builds the index with no key.

`export` builds the index once more into a temporary folder, notes every cache entry the build reads, and packs exactly
those; `load` puts a pack into `index/cache/`. A pack names the models, the prompt version and the vocabulary it was
made with: a pack that no longer matches is refused rather than silently paid for.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..errors import ResumesError

FORMAT = 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fingerprint(cfg: Config) -> dict:
    from ..extract.llm import PROMPT_VERSION

    icfg = cfg.index
    return {
        "embedder": icfg.embedder,
        "dim": icfg.dim,
        "extractor": icfg.extractor,
        "prompt_version": PROMPT_VERSION,
        "vocab_sha256": _sha256(icfg.vocab),
    }


def export(cfg: Config, out: Path, minus: list[Path] | None = None, log=print) -> dict:
    import duckdb

    from .build import build

    with tempfile.TemporaryDirectory() as tmp:
        seen: dict = {"keys": set(), "files": set()}
        build(replace(cfg, index=replace(cfg.index, out=Path(tmp) / "index")), log=lambda *a: None, seen=seen)
        for other in minus or []:
            held_keys, held_files = _contents(other)
            seen["keys"] -= held_keys
            seen["files"] = {f for f in seen["files"] if Path(f).name not in held_files}
        con = duckdb.connect(str(cfg.index.cache / "embeddings.duckdb"), read_only=True)
        con.execute("CREATE TEMP TABLE want (key VARCHAR)")
        con.executemany("INSERT INTO want VALUES (?)", [(k,) for k in sorted(seen["keys"])])
        model = cfg.index.embedder
        vectors = Path(tmp) / "embeddings.parquet"
        con.execute(
            f"COPY (SELECT e.model, e.key, e.vec FROM emb e JOIN want w USING (key) WHERE e.model = ? ORDER BY e.key) "
            f"TO '{vectors}' (FORMAT parquet, COMPRESSION zstd)",
            [model],
        )
        n_vec = con.execute(f"SELECT count(*) FROM '{vectors}'").fetchone()[0]
        con.close()
        if n_vec != len(seen["keys"]):
            raise ResumesError("SEED_INCOMPLETE", f"the build read {len(seen['keys'])} vectors, the cache holds {n_vec} of them")
        manifest = {
            "format": FORMAT,
            "made": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **fingerprint(cfg),
            "extractions": len(seen["files"]),
            "vectors": n_vec,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, "w:gz") as tar:
            data = json.dumps(manifest, indent=1).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            tar.add(vectors, arcname="embeddings.parquet")
            for f in sorted(seen["files"]):
                tar.add(f, arcname=f"extract/{Path(f).name}")
    log(f"seed · {out}: {manifest['extractions']} extractions, {n_vec} vectors, {out.stat().st_size / 1e6:.1f} MB, sha256 {_sha256(out)}")
    return manifest


def _contents(pack: Path) -> tuple[set[str], set[str]]:
    import duckdb

    with tarfile.open(pack, "r:gz") as tar, tempfile.TemporaryDirectory() as tmp:
        files = {m.name[8:] for m in tar.getmembers() if m.name.startswith("extract/")}
        tar.extract("embeddings.parquet", tmp, filter="data")
        keys = {r[0] for r in duckdb.sql(f"SELECT key FROM '{Path(tmp) / 'embeddings.parquet'}'").fetchall()}
    return keys, files


def _fetch(source: str, sha256: str | None, log) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    if not source.startswith(("http://", "https://")):
        path = Path(source)
        if not path.is_file():
            raise ResumesError("SEED_NOT_FOUND", f"{source}: no such file")
    else:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "seed.tar.gz"
        log(f"seed · downloading {source} …")
        try:
            urllib.request.urlretrieve(source, path)
        except OSError as e:
            tmp.cleanup()
            raise ResumesError("SEED_DOWNLOAD_FAILED", f"{source}: {e}") from None
    if sha256 and _sha256(path) != sha256:
        raise ResumesError("SEED_CHECKSUM", f"{source}: sha256 {_sha256(path)}, expected {sha256}")
    return path, (tmp if source.startswith(("http://", "https://")) else None)


def load(cfg: Config, source: str, sha256: str | None = None, log=print) -> dict:
    import duckdb

    path, tmp = _fetch(source, sha256, log)
    try:
        with tarfile.open(path, "r:gz") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            want = fingerprint(cfg)
            differs = {k: (manifest.get(k), v) for k, v in want.items() if manifest.get(k) != v}
            if manifest.get("format") != FORMAT or differs:
                raise ResumesError("SEED_MISMATCH", "the pack was made with other settings: "
                                   + ", ".join(f"{k} {a!r} ≠ {b!r}" for k, (a, b) in differs.items()))
            cache = cfg.index.cache
            (cache / "extract").mkdir(parents=True, exist_ok=True)
            written = 0
            for m in tar.getmembers():
                if m.isfile() and m.name.startswith("extract/") and "/" not in m.name[8:] and m.name.endswith(".json"):
                    target = cache / "extract" / m.name[8:]
                    if not target.exists():
                        target.write_bytes(tar.extractfile(m).read())
                        written += 1
            with tempfile.TemporaryDirectory() as t2:
                tar.extract("embeddings.parquet", t2, filter="data")
                con = duckdb.connect(str(cache / "embeddings.duckdb"))
                con.execute("CREATE TABLE IF NOT EXISTS emb (model VARCHAR, key VARCHAR, vec FLOAT[], PRIMARY KEY (model, key))")
                before = con.execute("SELECT count(*) FROM emb").fetchone()[0]
                con.execute(f"INSERT OR IGNORE INTO emb SELECT model, key, vec FROM '{Path(t2) / 'embeddings.parquet'}'")
                added = con.execute("SELECT count(*) FROM emb").fetchone()[0] - before
                con.close()
    finally:
        if tmp:
            tmp.cleanup()
    log(f"seed · {manifest['extractions']} extractions ({written} new), {manifest['vectors']} vectors ({added} new) in {cfg.index.cache}")
    return {**manifest, "written": written, "added": added}
