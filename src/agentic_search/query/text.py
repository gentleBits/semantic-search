"""Free-text channel: the only query-time scan.

    strong = docs with a chunk that contains every query word (FTS, conjunctive; stopwords dropped first,
             because the index never holds them and a conjunctive match over one would find nothing)
    weak   = docs with a chunk whose cosine to the query embedding ≥ tau_text, minus strong

`tau_text` is lower than the term threshold `tau`: a short sentence sits further from a chunk than a long
term description does. Embeddings use a raw HTTPS call (no SDK import, to keep start-up fast).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from pyroaring import BitMap

from ..config import Config
from ..errors import ResumesError
from .index import Index

_WORD = re.compile(r"[a-z0-9]+")
OPENAI_URL = "https://api.openai.com/v1/embeddings"


@dataclass
class TextResult:
    query: str
    strong: BitMap
    weak: BitMap
    bm25: dict[int, float]
    cosine: dict[int, float]
    tau: float
    words: list[str]

    @property
    def members(self) -> BitMap:
        return self.strong | self.weak


class QueryEmbedder:
    """Embeds query strings with the index's embedder, cached by (model, dim, text)."""

    def __init__(self, spec: str, dim: int, cache_dir: Path | None, socket_path: Path | None = None) -> None:
        self.spec, self.dim = spec, dim
        self.kind, _, self.model = spec.partition(":")
        self.cache_dir = cache_dir / "queries" if cache_dir else None
        self.socket_path = socket_path

    def _compute(self, texts: list[str]) -> list[list[float]]:
        """A running `resumes serve` first (warm model, keep-alive connection), then a direct call."""
        if self.socket_path is not None:
            from ..serve import embed_via_socket

            vecs = embed_via_socket(self.socket_path, texts)
            if vecs is not None:
                return vecs
        if self.kind == "openai":
            return self._openai_many(texts)
        return [self._local(t) for t in texts]

    def _cache_path(self, text: str) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(f"{self.spec}|{self.dim}|{text}".encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{key}.json"

    def embed(self, text: str) -> list[float]:
        p = self._cache_path(text)
        if p is not None and p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))["vec"]
            except (ValueError, KeyError):
                pass
        vec = self._compute([text])[0]
        if p is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"spec": self.spec, "dim": self.dim, "text": text, "vec": vec}), encoding="utf-8")
            os.replace(tmp, p)
        return vec

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """The uncached texts go in one API call."""
        out: list[list[float] | None] = [None] * len(texts)
        todo: list[int] = []
        for i, t in enumerate(texts):
            p = self._cache_path(t)
            if p is not None and p.is_file():
                try:
                    out[i] = json.loads(p.read_text(encoding="utf-8"))["vec"]
                    continue
                except (ValueError, KeyError):
                    pass
            todo.append(i)
        if todo:
            vecs = self._compute([texts[i] for i in todo])
            for i, v in zip(todo, vecs):
                out[i] = v
                p = self._cache_path(texts[i])
                if p is not None:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"spec": self.spec, "dim": self.dim, "text": texts[i], "vec": v}), encoding="utf-8")
                    os.replace(tmp, p)
        return out  # type: ignore[return-value]

    def _openai(self, text: str) -> list[float]:
        return self._openai_many([text])[0]

    def _openai_many(self, texts: list[str]) -> list[list[float]]:
        import urllib.error
        import urllib.request

        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ResumesError("EMBEDDER_UNAVAILABLE", f"{self.spec}: OPENAI_API_KEY is not set; use --topic/--skill instead")
        body = json.dumps({"model": self.model or "text-embedding-3-large", "input": [t[:8000] for t in texts], "dimensions": self.dim}).encode("utf-8")
        req = urllib.request.Request(OPENAI_URL, data=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:120]
            raise ResumesError("EMBEDDER_UNAVAILABLE", f"{self.spec}: HTTP {e.code} {detail}; use --topic/--skill instead") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ResumesError("EMBEDDER_UNAVAILABLE", f"{self.spec}: {getattr(e, 'reason', e)}; use --topic/--skill instead") from None
        return [[float(x) for x in item["embedding"]] for item in sorted(data["data"], key=lambda d: d["index"])]

    def _local(self, text: str) -> list[float]:
        try:
            from ..index.embed import make_embedder

            return make_embedder(self.spec, self.dim, None).embed([text])[0]
        except Exception as e:  # no sentence-transformers or no model: `resumes serve` keeps one warm
            raise ResumesError("EMBEDDER_UNAVAILABLE", f"{self.spec}: {type(e).__name__}; use --topic/--skill instead") from None


class TextSearch:
    def __init__(self, index: Index, cfg: Config) -> None:
        self.index, self.cfg = index, cfg

    @cached_property
    def stopwords(self) -> set[str]:
        self.index.load_fts()
        try:
            return {r[0] for r in self.index.con.execute("SELECT sw FROM fts_main_chunks.stopwords").fetchall()}
        except Exception:
            return set()

    def words(self, text: str) -> list[str]:
        return [w for w in _WORD.findall(text.lower()) if w not in self.stopwords]

    def run(self, text: str) -> TextResult:
        index = self.index
        words = self.words(text)
        bm25: dict[int, float] = {}
        if words:
            index.load_fts()
            rows = index.con.execute(
                "SELECT doc_no, max(s) FROM (SELECT doc_no, fts_main_chunks.match_bm25(chunk_id, ?, conjunctive := 1) AS s FROM chunks) "
                "WHERE s IS NOT NULL GROUP BY doc_no",
                [" ".join(words)],
            ).fetchall()
            bm25 = {int(d): float(s) for d, s in rows}
        spec = index.meta.get("embedder") or self.cfg.index.embedder
        vec = QueryEmbedder(spec, index.dim, self.cfg.index.cache, self.cfg.query.serve_socket).embed(text)
        rows = index.con.execute(
            f"SELECT doc_no, max(array_cosine_similarity(emb, $1::FLOAT[{index.dim}])) FROM chunks WHERE section <> 'education' GROUP BY doc_no",
            [vec],
        ).fetchall()
        cosine = {int(d): float(s) for d, s in rows}
        tau = self.cfg.query.tau_text
        strong = BitMap(bm25)
        weak = BitMap(d for d, s in cosine.items() if s >= tau) - strong
        return TextResult(text, strong, weak, bm25, cosine, tau, words)


def search_text(index: Index, cfg: Config, text: str) -> TextResult:
    return TextSearch(index, cfg).run(text)
