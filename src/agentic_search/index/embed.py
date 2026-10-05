"""Embedders: OpenAI by default, local sentence-transformers as the offline option,
both behind a content-hash cache in DuckDB so that rebuilds never re-embed unchanged text.

Spec strings: "openai:text-embedding-3-large", "local:BAAI/bge-m3".
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OPENAI_MAX_TOKENS = 8000  # text-embedding-3-* accept 8,191 tokens per input


class Embedder:
    model_id: str
    dim: int

    def count_tokens(self, text: str) -> int:
        raise NotImplementedError

    def truncate(self, text: str, max_tokens: int) -> str:
        raise NotImplementedError

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class OpenAIEmbedder(Embedder):
    def __init__(self, model: str = "text-embedding-3-large", dim: int = 1024, batch: int = 64, workers: int = 8) -> None:
        import tiktoken

        self.model_id = f"openai:{model}"
        self.model = model
        self.dim = dim
        self.batch = batch
        self.workers = workers
        self._client = None
        self.enc = tiktoken.get_encoding("cl100k_base")

    @property
    def client(self):
        if self._client is None:
            from ..keys import openai_client

            self._client = openai_client(f"embedding text that is not in the cache ({self.model})")
        return self._client

    def count_tokens(self, text: str) -> int:
        return len(self.enc.encode(text, disallowed_special=()))

    def truncate(self, text: str, max_tokens: int) -> str:
        ids = self.enc.encode(text, disallowed_special=())
        return text if len(ids) <= max_tokens else self.enc.decode(ids[:max_tokens])

    def _call(self, batch: list[str]) -> list[list[float]]:
        for attempt in range(6):
            try:
                r = self.client.embeddings.create(model=self.model, input=batch, dimensions=self.dim)
                return [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
            except Exception as e:  # rate limits / transient network; a wrong key is not retried
                if attempt == 5 or getattr(e, "status_code", None) == 401:
                    raise
                time.sleep(min(30, 2**attempt) + 0.1)
        raise RuntimeError("unreachable")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        texts = [self.truncate(t if t.strip() else " ", OPENAI_MAX_TOKENS) for t in texts]
        batches = [texts[i : i + self.batch] for i in range(0, len(texts), self.batch)]
        self.client  # a missing key fails here, once, not in every worker
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            results = list(ex.map(self._call, batches))
        return [v for r in results for v in r]


class LocalEmbedder(Embedder):
    """sentence-transformers model (e.g. BAAI/bge-m3). Loaded lazily; nothing leaves the machine."""

    def __init__(self, model: str = "BAAI/bge-m3", dim: int = 1024) -> None:
        from sentence_transformers import SentenceTransformer  # optional dependency

        self.model_id = f"local:{model}"
        self.dim = dim
        self.st = SentenceTransformer(model)
        self.tok = self.st.tokenizer

    def count_tokens(self, text: str) -> int:
        return len(self.tok.encode(text, add_special_tokens=False))

    def truncate(self, text: str, max_tokens: int) -> str:
        ids = self.tok.encode(text, add_special_tokens=False)
        return text if len(ids) <= max_tokens else self.tok.decode(ids[:max_tokens])

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self.st.encode(texts, normalize_embeddings=True, batch_size=32)
        return [[float(x) for x in v[: self.dim]] for v in vecs]


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class CachedEmbedder(Embedder):
    """Wraps an embedder with a DuckDB cache keyed by (model, sha256(text))."""

    def __init__(self, inner: Embedder, cache_dir: Path) -> None:
        import duckdb

        self.inner = inner
        self.model_id = inner.model_id
        self.dim = inner.dim
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(cache_dir / "embeddings.duckdb"))
        self.con.execute("CREATE TABLE IF NOT EXISTS emb (model VARCHAR, key VARCHAR, vec FLOAT[], PRIMARY KEY (model, key))")
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.seen: set[str] = set()  # every key asked for: what a seed pack must hold

    def count_tokens(self, text: str) -> int:
        return self.inner.count_tokens(text)

    def truncate(self, text: str, max_tokens: int) -> str:
        return self.inner.truncate(text, max_tokens)

    SLICE = 2000  # texts per lookup/embed/store round: progress is visible and an interrupted run keeps its work

    def _lookup(self, keys: list[str]) -> dict[str, list[float]]:
        with self._lock:
            self.con.execute("CREATE OR REPLACE TEMP TABLE want (key VARCHAR)")
            self.con.executemany("INSERT INTO want VALUES (?)", [(k,) for k in set(keys)])
            rows = self.con.execute(
                "SELECT e.key, e.vec FROM emb e JOIN want w ON e.key = w.key WHERE e.model = ?", [self.model_id]
            ).fetchall()
        return {k: list(v) for k, v in rows}

    def _store(self, keys: list[str], vecs: list[list[float]]) -> None:
        from .store import bulk_insert

        rows = [{"model": self.model_id, "key": k, "vec": v} for k, v in zip(keys, vecs)]
        with self._lock:
            bulk_insert(self.con, "emb", "model, key, vec::FLOAT[]", rows, self.cache_dir, insert="INSERT OR IGNORE INTO")

    def embed(self, texts: list[str], log=None) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float] | None] = [None] * len(texts)
        t0 = time.time()
        for start in range(0, len(texts), self.SLICE):
            sl = texts[start : start + self.SLICE]
            keys = [_key(t) for t in sl]
            self.seen.update(keys)
            found = self._lookup(keys)
            uniq: dict[str, str] = {}
            for k, t in zip(keys, sl):
                if k not in found:
                    uniq.setdefault(k, t)
            self.hits += sum(1 for k in keys if k in found)
            self.misses += len(uniq)
            if uniq:
                new_keys = list(uniq)
                vecs = self.inner.embed([uniq[k] for k in new_keys])
                self._store(new_keys, vecs)
                found.update(zip(new_keys, vecs))
            for i, k in enumerate(keys):
                out[start + i] = found[k]
            if log and len(texts) > self.SLICE:
                log(f"    embedded {min(start + self.SLICE, len(texts))}/{len(texts)} ({time.time() - t0:.0f}s, cache hits {self.hits})")
        return out  # type: ignore[return-value]


class FakeEmbedder(Embedder):
    """Deterministic hash vectors; for protocol tests only (spec `fake:<anything>`)."""

    def __init__(self, dim: int) -> None:
        self.model_id, self.dim = "fake", dim

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def truncate(self, text: str, max_tokens: int) -> str:
        return text[: max_tokens * 4]

    def embed(self, texts: list[str]) -> list[list[float]]:
        import random

        out = []
        for t in texts:
            rng = random.Random(hashlib.sha256(t.encode("utf-8")).hexdigest())
            v = [rng.uniform(-1, 1) for _ in range(self.dim)]
            n = sum(x * x for x in v) ** 0.5
            out.append([x / n for x in v])
        return out


def make_embedder(spec: str, dim: int, cache_dir: Path | None) -> Embedder:
    kind, _, model = spec.partition(":")
    if kind == "fake":
        return FakeEmbedder(dim)
    if kind == "openai":
        inner: Embedder = OpenAIEmbedder(model or "text-embedding-3-large", dim)
    elif kind == "local":
        inner = LocalEmbedder(model or "BAAI/bge-m3", dim)
    else:
        raise ValueError(f"unknown embedder spec {spec!r} (expected openai:<model> or local:<model>)")
    return CachedEmbedder(inner, cache_dir) if cache_dir else inner


def token_counter(spec: str) -> Callable[[str], int]:
    """Token counter for chunking without constructing an API client."""
    if spec.startswith("openai:"):
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return lambda t: len(enc.encode(t, disallowed_special=()))
    return lambda t: max(1, len(t) // 4)
