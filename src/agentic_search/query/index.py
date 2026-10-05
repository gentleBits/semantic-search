"""Read-only access to the built index. The vocabulary is read from the index itself, so phrases resolve
against the terms the bitmaps were built with."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import duckdb
from pyroaring import BitMap

from ..config import Config
from ..errors import ResumesError
from ..index.store import open_current, read_meta
from ..vocab.resolve import Term, Vocabulary


@dataclass
class Profile:
    doc_no: int
    id: str
    title: str | None
    seniority: str | None
    years: float | None
    rate: float | None
    currency: str | None
    rate_source: str | None
    location: str | None
    remote: bool | None
    availability: str | None
    path: str
    deleted: bool
    dup_group: int | None


def _in_list(doc_nos) -> str:
    """`IN (1,2,…)` inlined: a list parameter makes DuckDB try to import pandas per element (~30x slower)."""
    ids = ",".join(str(int(d)) for d in doc_nos)
    return f"IN ({ids})" if ids else "IN (NULL)"


class Index:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._bitmaps: dict[str, tuple[BitMap, BitMap, BitMap]] = {}
        self._fts_loaded = False

    # ------------------------------------------------------------ connection and metadata
    @cached_property
    def con(self) -> duckdb.DuckDBPyConnection:
        try:
            return open_current(self.cfg.index.out)
        except FileNotFoundError:
            raise ResumesError("INDEX_NOT_BUILT", "run `resumes index build`") from None

    @cached_property
    def meta(self) -> dict[str, str]:
        return read_meta(self.con)

    @property
    def version(self) -> str:
        return self.meta["index_version"]

    @property
    def dim(self) -> int:
        return int(self.meta["dim"])

    @property
    def tau(self) -> float:
        return float(self.meta["tau"])

    def load_fts(self) -> None:
        if not self._fts_loaded:
            try:
                self.con.execute("LOAD fts;")
            except duckdb.Error:
                self.con.execute("INSTALL fts; LOAD fts;")
            self._fts_loaded = True

    # ------------------------------------------------------------ vocabulary and bitmaps
    @cached_property
    def vocab(self) -> Vocabulary:
        rows = self.con.execute(
            "SELECT term_id, kind, slug, canonical, aliases, implies, ambiguous, description FROM terms ORDER BY term_id"
        ).fetchall()
        terms = [
            Term(term_id=r[0], kind=r[1], slug=r[2], canonical=r[3], aliases=list(r[4] or []), implies=list(r[5] or []),
                 ambiguous={a.lower() for a in (r[6] or [])}, context=set(), description=r[7] or "")
            for r in rows
        ]
        return Vocabulary(terms)

    @cached_property
    def vocab_with_context(self) -> Vocabulary:
        """`vocab` plus context words (for `--like`), from schema/vocab.csv when its hash matches the index."""
        import hashlib

        path = self.cfg.index.vocab
        try:
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()[:12] == self.meta.get("vocab_hash"):
                return Vocabulary.load(path)
        except (OSError, ValueError):
            pass
        return self.vocab

    def term(self, slug: str) -> Term:
        t = self.vocab.by_slug.get(slug)
        if t is None:
            raise ResumesError("UNRESOLVED_TERM", f"{slug!r} is not in the vocabulary")
        return t

    def bitmaps(self, slug: str) -> tuple[BitMap, BitMap, BitMap]:
        """(strong, used, weak) membership of one term."""
        if slug not in self._bitmaps:
            row = self.con.execute(
                "SELECT strong_bm, used_bm, weak_bm FROM postings p JOIN terms t USING (term_id) WHERE t.slug = ?", [slug]
            ).fetchone()
            if row is None:
                self._bitmaps[slug] = (BitMap(), BitMap(), BitMap())
            else:
                self._bitmaps[slug] = tuple(BitMap.deserialize(bytes(b)) for b in row)  # type: ignore[assignment]
        return self._bitmaps[slug]

    def member_scores(self, slug: str) -> dict[int, tuple[int, float, float]]:
        """doc_no → (evidence rank, bm25, cosine) for every member of the term."""
        rows = self.con.execute(
            "SELECT ms.doc_no, ms.evidence_rank, ms.bm25, ms.cosine FROM member_scores ms JOIN terms t USING (term_id) WHERE t.slug = ?", [slug]
        ).fetchall()
        return {r[0]: (int(r[1]), float(r[2] or 0.0), float(r[3] or 0.0)) for r in rows}

    # ------------------------------------------------------------ documents
    @cached_property
    def all_docs(self) -> BitMap:
        return BitMap(r[0] for r in self.con.execute("SELECT doc_no FROM docs WHERE NOT deleted").fetchall())

    @cached_property
    def dup_groups(self) -> dict[int, list[int]]:
        """dup_group → doc_nos, ascending."""
        rows = self.con.execute("SELECT dup_group, list(doc_no ORDER BY doc_no) FROM docs WHERE dup_group IS NOT NULL GROUP BY 1").fetchall()
        return {r[0]: list(r[1]) for r in rows}

    @cached_property
    def dup_group_of(self) -> dict[int, int]:
        return {d: g for g, docs in self.dup_groups.items() for d in docs}

    @cached_property
    def representatives(self) -> BitMap:
        """One document per person: the newest version of each CV."""
        alive = self.all_docs
        older = BitMap()
        for docs in self.dup_groups.values():
            versions = [d for d in docs if d in alive]
            older.update(versions[:-1])                 # ascending: the last one is the newest
        return alive - older

    def profiles(self, doc_nos: list[int]) -> dict[int, Profile]:
        if not doc_nos:
            return {}
        rows = self.con.execute(
            "SELECT p.doc_no, d.id, coalesce(p.current_title, p.headline), p.seniority, p.years, p.rate, p.currency, p.rate_source, "
            "p.location, p.remote, p.availability, d.path, d.deleted, d.dup_group "
            f"FROM profile p JOIN docs d USING (doc_no) WHERE p.doc_no {_in_list(doc_nos)}"
        ).fetchall()
        return {r[0]: Profile(*r) for r in rows}

    def cards(self, doc_nos: list[int]) -> dict[int, str]:
        if not doc_nos:
            return {}
        rows = self.con.execute(f"SELECT doc_no, text FROM cards WHERE doc_no {_in_list(doc_nos)}").fetchall()
        return dict(rows)

    def doc_nos(self, ids: list[str]) -> dict[str, int]:
        if not ids:
            return {}
        rows = self.con.execute("SELECT id, doc_no FROM docs WHERE id IN (SELECT unnest($1::VARCHAR[]))", [list(ids)]).fetchall()
        return dict(rows)

    def ids(self, doc_nos: list[int]) -> dict[int, str]:
        if not doc_nos:
            return {}
        rows = self.con.execute(f"SELECT doc_no, id FROM docs WHERE doc_no {_in_list(doc_nos)}").fetchall()
        return dict(rows)

    def doc(self, ref: str) -> Profile:
        """One document by id (r000412) or doc_no (412)."""
        ref = ref.strip()
        row = self.con.execute("SELECT doc_no FROM docs WHERE id = ? OR (? AND doc_no = ?)",
                               [ref, ref.isdigit(), int(ref) if ref.isdigit() else -1]).fetchone()
        if row is None:
            raise ResumesError("UNKNOWN_DOC_ID", ref)
        return self.profiles([row[0]])[row[0]]

    def markdown(self, doc_no: int) -> str | None:
        row = self.con.execute("SELECT markdown FROM docs WHERE doc_no = ?", [doc_no]).fetchone()
        return row[0] if row else None

    def link(self, p: Profile) -> str:
        tpl = self.cfg.query.link_template if self.cfg.query else None
        if tpl:
            return tpl.format(id=p.id, doc_no=p.doc_no)
        return (self.cfg.root / p.path).resolve().as_uri()
