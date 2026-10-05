"""Judgments: the agent's scores, validated and stored per session.

`judgments.parquet` has one row per document per judgment: judgment_id, set_id, criterion, doc_no,
score (0–100 or null), note, judge, created_at. Every `score` call rewrites the whole file (temp file,
then rename). Each accepted scores file is kept verbatim as scores/j_NN.json.

A score belongs to a person and a criterion, not to a set: a later pass under the same criterion
(`into="j_01"`, kept as scores/j_01+rs_07.json) adds the people not judged yet; existing scores stand.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass

from ..errors import ResumesError
from .store import ResultSet, Session, now_iso

NOTE_MAX = 120
COLUMNS = "judgment_id VARCHAR, set_id VARCHAR, criterion VARCHAR, doc_no INTEGER, score INTEGER, note VARCHAR, judge VARCHAR, created_at VARCHAR"


@dataclass
class ScoreStats:
    judgment_id: str
    set_id: str
    criterion: str
    judge: str
    n_scored: int
    n_missing: int
    n_clamped: int
    n_truncated: int
    p50: float | None
    top: tuple[str, int] | None      # (doc id, score) of the best-scored document
    n_kept: int = 0                  # merged pass: people who already had a score under this criterion (kept)
    merged: bool = False


def _payload_from(text: str) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ResumesError("SCORES_INVALID", f"not JSON: {e.msg} (line {e.lineno})") from None
    if isinstance(payload, list):
        payload = {"scores": payload}
    if not isinstance(payload, dict) or not isinstance(payload.get("scores"), list):
        raise ResumesError("SCORES_INVALID", 'expected {"criterion": …, "judge": …, "scores": [{"id", "score", "note"}, …]}')
    return payload


def validate(payload: dict, rs: ResultSet, id_to_docno: dict[str, int], allow_partial: bool,
             *, criterion: str | None = None, judge: str | None = None,
             already: dict[int, tuple[int | None, str]] | None = None) -> tuple[list[tuple[int, int | None, str]], dict]:
    """Rows (doc_no, score, note) for every member of the set, plus counters. Raises on bad input.

    `already` (a merged pass): the scores the judgment holds. People in it are skipped, whatever the file
    says about them, and only the members it does not cover have to be scored."""
    members = set(rs.order)
    judged = {d for d, (s, _) in (already or {}).items() if s is not None}
    n_kept = 0
    docno_to_id = {v: k for k, v in id_to_docno.items()}
    criterion = (criterion or payload.get("criterion") or "").strip()
    judge = (judge or payload.get("judge") or "unknown").strip()
    if not criterion:
        raise ResumesError("SCORES_INVALID", 'a "criterion" is required: write down what the scores mean')
    seen: dict[int, tuple[int | None, str]] = {}
    unknown: list[str] = []
    dupes: list[str] = []
    n_clamped = n_truncated = 0
    for i, entry in enumerate(payload["scores"]):
        if not isinstance(entry, dict):
            raise ResumesError("SCORES_INVALID", f"scores[{i}] is not an object")
        raw_id = entry.get("id", entry.get("doc_no"))
        if raw_id is None:
            raise ResumesError("SCORES_INVALID", f'scores[{i}] has no "id"')
        raw_id = str(raw_id).strip()
        doc_no = id_to_docno.get(raw_id)
        if doc_no is None and raw_id.isdigit():
            doc_no = int(raw_id)
        if doc_no is not None and doc_no in judged:          # e.g. an anchor card echoed back: its score stands
            n_kept += 1
            continue
        if doc_no is None or doc_no not in members:
            unknown.append(raw_id)
            continue
        if doc_no in seen:
            dupes.append(docno_to_id.get(doc_no, raw_id))
            continue
        score = entry.get("score")
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                raise ResumesError("SCORES_INVALID", f"scores[{i}] ({raw_id}): score {score!r} is not a number") from None
            if score < 0 or score > 100:
                n_clamped += 1
            score = int(round(max(0.0, min(100.0, score))))
        note = str(entry.get("note") or "").strip().replace("\n", " ")
        if len(note) > NOTE_MAX:
            note = note[: NOTE_MAX - 1].rstrip() + "…"
            n_truncated += 1
        seen[doc_no] = (score, note)
    if unknown:
        raise ResumesError("SCORES_UNKNOWN_ID", f"{len(unknown)} not in {rs.id}: {', '.join(unknown[:5])}")
    if dupes:
        raise ResumesError("SCORES_DUPLICATE", f"{len(dupes)} scored twice: {', '.join(dupes[:5])}")
    missing = [d for d in rs.order if d not in seen and d not in judged]
    if missing and not allow_partial:
        ids = ", ".join(docno_to_id.get(d, str(d)) for d in missing[:5])
        more = f", +{len(missing) - 5} more" if len(missing) > 5 else ""
        raise ResumesError("SCORES_INCOMPLETE", f"{len(missing)} ids missing: {ids}{more} (or pass --allow-partial)")
    known = set(already or {})
    rows = [(d, *seen.get(d, (None, ""))) for d in rs.order if d in seen or (d not in judged and d not in known)]
    info = {"criterion": criterion, "judge": judge, "n_scored": len(seen), "n_missing": len(missing),
            "n_clamped": n_clamped, "n_truncated": n_truncated, "n_kept": n_kept}
    return rows, info


def criterion_of(session: Session, jid: str, con=None) -> str | None:
    return next((j["criterion"] for j in list_judgments(session, con) if j["id"] == jid), None)


def same_criterion(a: str | None, b: str | None) -> bool:
    norm = lambda s: " ".join((s or "").lower().split())      # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def record(session: Session, rs: ResultSet, text: str, id_to_docno: dict[str, int], *, allow_partial: bool = False,
           criterion: str | None = None, judge: str | None = None, into: str | None = None) -> ScoreStats:
    """Validate a scores file and store it: as the next judgment on `rs`, or merged into the judgment `into`."""
    import duckdb

    payload = _payload_from(text)
    already = None
    if into:
        already = load(session, into)
        kept_criterion = criterion_of(session, into)
        if not already or kept_criterion is None:
            have = ", ".join(sorted({j["id"] for j in list_judgments(session)})) or "none"
            raise ResumesError("UNKNOWN_JUDGMENT", f"{into} (have {have})")
        criterion = kept_criterion                 # the criterion of a judgment never changes
    rows, info = validate(payload, rs, id_to_docno, allow_partial, criterion=criterion, judge=judge, already=already)
    jid = into or session.next_judgment_id()
    created = now_iso()
    full = [(jid, rs.id, info["criterion"], d, s, n, info["judge"], created) for d, s, n in rows]

    con = duckdb.connect()
    con.execute(f"CREATE TABLE j ({COLUMNS})")
    if session.judgments_path.is_file():
        con.execute("INSERT INTO j SELECT * FROM read_parquet(?)", [str(session.judgments_path)])
    con.executemany("INSERT INTO j VALUES (?, ?, ?, ?, ?, ?, ?, ?)", full)
    fd, tmp = tempfile.mkstemp(prefix="judgments.", suffix=".parquet.tmp", dir=session.dir)
    os.close(fd)
    try:
        con.execute(f"COPY j TO '{tmp}' (FORMAT PARQUET)")
        os.replace(tmp, session.judgments_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        con.close()

    session.scores_dir.mkdir(parents=True, exist_ok=True)
    audit = f"{jid}+{rs.id}.json" if into else f"{jid}.json"
    (session.scores_dir / audit).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")

    scored = [(d, s) for d, s, _ in rows if s is not None]
    docno_to_id = {v: k for k, v in id_to_docno.items()}
    p50 = None
    top = None
    if scored:
        vals = sorted(s for _, s in scored)
        p50 = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
        best = max(scored, key=lambda x: (x[1], -x[0]))
        top = (docno_to_id.get(best[0], str(best[0])), best[1])
    return ScoreStats(jid, rs.id, info["criterion"], info["judge"], info["n_scored"], info["n_missing"],
                      info["n_clamped"], info["n_truncated"], p50, top, info["n_kept"], bool(into))


def load(session: Session, jid: str, con=None) -> dict[int, tuple[int | None, str]]:
    """doc_no → (score, note) for one judgment. Empty when the file does not exist."""
    if not session.judgments_path.is_file():
        return {}
    con = con or _mem()
    rows = con.execute("SELECT doc_no, score, note FROM read_parquet(?) WHERE judgment_id = ?", [str(session.judgments_path), jid]).fetchall()
    out: dict[int, tuple[int | None, str]] = {}
    for d, s, n in rows:                       # a merged judgment can hold an unscored row and a later score for one person
        if d not in out or (out[d][0] is None and s is not None):
            out[d] = (s, n or "")
    return out


def list_judgments(session: Session, con=None) -> list[dict]:
    if not session.judgments_path.is_file():
        return []
    con = con or _mem()
    rows = con.execute(
        "SELECT judgment_id, set_id, any_value(criterion), any_value(judge), any_value(created_at), count(*), count(score) "
        "FROM read_parquet(?) GROUP BY 1, 2 ORDER BY 1", [str(session.judgments_path)]
    ).fetchall()
    return [{"id": r[0], "set_id": r[1], "criterion": r[2], "judge": r[3], "created_at": r[4], "n": r[5], "n_scored": r[6]} for r in rows]


def newest_on_lineage(session: Session, lineage: list[str], con=None) -> str | None:
    """The newest judgment made on the set or any ancestor (judgments are keyed by document, so they travel with subsets)."""
    js = [j for j in list_judgments(session, con) if j["set_id"] in set(lineage)]
    if not js:
        return None
    return max(js, key=lambda j: int(j["id"].split("_", 1)[1]))["id"]


def _mem():
    import duckdb

    return duckdb.connect()
