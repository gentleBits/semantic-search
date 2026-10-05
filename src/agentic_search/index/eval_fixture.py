"""`resumes index eval-fixture`: membership precision/recall against data/fixture/ground_truth.json.

Exact ground truth: data-pipelines (topic) and elixir (skill). Every other persona topic
is a guarantee of presence only, so it is reported as recall.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
from pyroaring import BitMap

from ..config import Config
from .store import open_current


def _bitmaps(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[BitMap, BitMap, BitMap]]:
    out = {}
    for slug, s, u, w in con.execute(
        "SELECT t.slug, p.strong_bm, p.used_bm, p.weak_bm FROM postings p JOIN terms t USING (term_id)"
    ).fetchall():
        out[slug] = (BitMap.deserialize(bytes(s)), BitMap.deserialize(bytes(u)), BitMap.deserialize(bytes(w)))
    return out


def _prf(members: set[int], truth: set[int], universe: set[int]) -> dict:
    tp = len(members & truth)
    fp = len((members & universe) - truth)
    fn = len(truth - members)
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(p, 3), "recall": round(r, 3)}


def evaluate(cfg: Config, con: duckdb.DuckDBPyConnection | None = None) -> dict:
    con = con or open_current(cfg.index.out)
    gt = json.loads((cfg.root / "data" / "fixture" / "ground_truth.json").read_text(encoding="utf-8"))["docs"]
    fixture_docs = {sid: dn for dn, sid in con.execute("SELECT doc_no, source_id FROM docs WHERE source = 'fixture'").fetchall()}
    universe = set(fixture_docs.values())
    bm = _bitmaps(con)

    def members(slug: str, mode: str) -> set[int]:
        s, u, w = bm[slug]
        if mode == "strong":
            return set(s)
        if mode == "used":
            return set(u)
        return set(s) | set(w)

    report: dict = {"fixture_docs": len(universe)}

    # data-pipelines: exact truth; the topic uses strong ∪ weak by default
    truth = {fixture_docs[sid] for sid, g in gt.items() if g["pipeline"]}
    para = {fixture_docs[sid] for sid, g in gt.items() if g["pipeline_evidence"] == "paraphrase"}
    for mode in ("strong", "any"):
        m = members("data-pipelines", mode)
        r = _prf(m, truth, universe)
        r["paraphrase_recall"] = round(len(m & para) / len(para), 3) if para else None
        r["false_positive_ids"] = sorted(sid for sid, dn in fixture_docs.items() if dn in m and dn not in truth)[:15]
        report[f"data-pipelines/{mode}"] = r

    # elixir: exact truth; skills use strong only
    truth_e = {fixture_docs[sid] for sid, g in gt.items() if g["elixir"] != "none"}
    m = members("elixir", "strong")
    r = _prf(m, truth_e, universe)
    controls = {fixture_docs[sid] for sid, g in gt.items() if g["negative_control"]}
    r["negative_controls_included"] = len(m & controls)
    r["phoenix_only_found"] = sum(1 for sid, g in gt.items() if g["elixir"] == "phoenix_only" and fixture_docs[sid] in m)
    r["phoenix_only_total"] = sum(1 for g in gt.values() if g["elixir"] == "phoenix_only")
    r["false_negative_ids"] = sorted(sid for sid, g in gt.items() if g["elixir"] != "none" and fixture_docs[sid] not in m)[:15]
    report["elixir/strong"] = r
    report["elixir_and_pipelines/any"] = _prf(members("elixir", "strong") & members("data-pipelines", "any"),
                                            truth_e & truth, universe)

    # every other persona topic: presence is guaranteed → recall
    recalls = {}
    for slug in sorted({t for g in gt.values() for t in g["topics"] if t != "data-pipelines"}):
        want = {fixture_docs[sid] for sid, g in gt.items() if slug in g["topics"]}
        if slug in bm and want:
            recalls[slug] = {"n": len(want), "recall_any": round(len(members(slug, "any") & want) / len(want), 3),
                             "recall_strong": round(len(members(slug, "strong") & want) / len(want), 3)}
    report["topic_recall"] = recalls

    # the Java JD candidates: B must be in rest-apis via "web services"; E must be listed-only for java
    specials = {g["special"]: fixture_docs[sid] for sid, g in gt.items() if g["special"] in ("B", "E", "D")}
    b_dn, e_dn = specials.get("B"), specials.get("E")
    report["B_in_rest_apis"] = b_dn in members("rest-apis", "strong")
    e_level = con.execute(
        "SELECT dt.level FROM doc_terms dt JOIN terms t USING (term_id) WHERE dt.doc_no = ? AND t.slug = 'java'", [e_dn]
    ).fetchone()
    report["E_java_level"] = e_level[0] if e_level else None
    report["E_in_java_used"] = e_dn in members("java", "used")
    report["tau_sweep"] = tau_sweep(con, gt, fixture_docs, members("data-pipelines", "strong"), universe)
    return report


def tau_sweep(con, gt: dict, fixture_docs: dict[str, int], strong: set[int], universe: set[int]) -> dict:
    """data-pipelines precision/recall of strong ∪ weak(τ) for several τ, from the chunk embeddings directly."""
    import numpy as np

    truth = {fixture_docs[sid] for sid, g in gt.items() if g["pipeline"]}
    para = {fixture_docs[sid] for sid, g in gt.items() if g["pipeline_evidence"] == "paraphrase"}
    tv = np.asarray(con.execute("SELECT emb FROM terms WHERE slug = 'data-pipelines'").fetchone()[0], dtype=np.float32)
    rows = con.execute(
        "SELECT c.doc_no, max(array_cosine_similarity(c.emb, t.emb)) FROM chunks c, (SELECT emb FROM terms WHERE slug='data-pipelines') t "
        "WHERE c.section <> 'education' AND c.doc_no IN (SELECT doc_no FROM docs WHERE source = 'fixture') GROUP BY c.doc_no"
    ).fetchall()
    best = {d: float(v) for d, v in rows}
    out = {}
    for tau in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        weak = {d for d, v in best.items() if v >= tau}
        m = strong | weak
        r = _prf(m, truth, universe)
        r["paraphrase_recall"] = round(len(m & para) / len(para), 3) if para else None
        r["weak_only"] = len(weak - strong)
        out[str(tau)] = r
    return out
