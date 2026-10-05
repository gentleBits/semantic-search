"""Open the current index read-only; schema DDL; atomic swap of `index/current`."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

SCHEMA_VERSION = 1


def ddl(dim: int) -> str:
    return f"""
CREATE TABLE docs (
  doc_no INTEGER PRIMARY KEY, id VARCHAR, hash VARCHAR, source VARCHAR, source_id VARCHAR, category VARCHAR,
  path VARCHAR, markdown VARCHAR, words INTEGER, added_at TIMESTAMP, deleted BOOLEAN DEFAULT false, dup_group INTEGER
);
CREATE TABLE profile (
  doc_no INTEGER PRIMARY KEY, headline VARCHAR, current_title VARCHAR, seniority VARCHAR,
  years DOUBLE, years_source VARCHAR, rate DOUBLE, currency VARCHAR, rate_source VARCHAR,
  location VARCHAR, remote BOOLEAN, availability VARCHAR, education VARCHAR,
  did VARCHAR, did_source VARCHAR, summary VARCHAR, extracted_by VARCHAR, extracted_at TIMESTAMP
);
CREATE TABLE terms (
  term_id INTEGER PRIMARY KEY, kind VARCHAR, slug VARCHAR UNIQUE, canonical VARCHAR, aliases VARCHAR[],
  implies VARCHAR[], ambiguous VARCHAR[], description VARCHAR, emb FLOAT[{dim}]
);
CREATE TABLE doc_terms (
  doc_no INTEGER, term_id INTEGER, level VARCHAR, via VARCHAR, evidence VARCHAR, section VARCHAR,
  PRIMARY KEY (doc_no, term_id)
);
CREATE TABLE postings (
  term_id INTEGER PRIMARY KEY, strong_bm BLOB, used_bm BLOB, weak_bm BLOB, n_strong INTEGER, n_used INTEGER, n_weak INTEGER
);
CREATE TABLE member_scores (
  term_id INTEGER, doc_no INTEGER, evidence_rank SMALLINT, bm25 REAL, cosine REAL, PRIMARY KEY (term_id, doc_no)
);
CREATE TABLE chunks (
  chunk_id INTEGER PRIMARY KEY, doc_no INTEGER, idx INTEGER, section VARCHAR, header VARCHAR, text VARCHAR,
  tokens INTEGER, emb FLOAT[{dim}]
);
CREATE TABLE cards (doc_no INTEGER PRIMARY KEY, text VARCHAR, tokens SMALLINT);
CREATE TABLE meta (key VARCHAR PRIMARY KEY, value VARCHAR);
"""


def current_path(index_dir: Path) -> Path | None:
    link = index_dir / "current"
    if not link.exists():
        return None
    return link.resolve()


def open_current(index_dir: Path) -> duckdb.DuckDBPyConnection:
    p = current_path(index_dir)
    if p is None or not p.is_file():
        raise FileNotFoundError("INDEX_NOT_BUILT: run `resumes index build`")
    return duckdb.connect(str(p), read_only=True)


def swap_current(index_dir: Path, new_file: Path) -> None:
    """Atomically repoint index/current at new_file (symlink replaced with os.replace)."""
    tmp = index_dir / "current.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(new_file.name, tmp)
    os.replace(tmp, index_dir / "current")


def read_meta(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    return dict(con.execute("SELECT key, value FROM meta").fetchall())


def bulk_insert(con: duckdb.DuckDBPyConnection, table: str, select_cols: str, rows: list[dict], tmp_dir: Path, insert: str = "INSERT INTO") -> int:
    """Insert many rows fast: write JSON lines, let DuckDB's vectorised reader load them.

    `select_cols` lists the columns in table order with any casts, e.g. "chunk_id, doc_no, emb::FLOAT[1024]".
    Row-by-row executemany takes minutes for 25k rows of 1,024-float vectors; this takes seconds.
    """
    import json
    import os
    import tempfile

    if not rows:
        return 0
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".jsonl", dir=tmp_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        con.execute(
            f"{insert} {table} SELECT {select_cols} FROM read_json_auto(?, format='newline_delimited', maximum_object_size=4000000)",
            [path],
        )
    finally:
        os.unlink(path)
    return len(rows)
