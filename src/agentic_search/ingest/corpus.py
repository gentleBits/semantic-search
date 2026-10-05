"""`resumes corpus build`: sources → canonical markdown in corpus/md/, plus registry and stats."""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..config import Config, Source
from ..errors import ResumesError
from . import csv_kaggle, csv_livecareer, markdown_dir
from .dedupe import NEAR_DUP_THRESHOLD, content_hash, near_duplicate_groups
from .normalize import normalize
from .rates import seniority_hint, synthetic_rate
from .redact import RedactionCounts, redact
from .registry import Registry, doc_id
from .types import RawDoc

_HTML_TAG = re.compile(r"</?(?:div|span|p|ul|li|br|table|td|tr|b|i|u|font)\b[^>]*>", re.I)
_MOJIBAKE = re.compile(r"Ã.|â€|â¢|Â")
PASS_THROUGH_META = ("name", "location", "remote", "availability", "years", "seniority", "title")
MIN_WORDS = 5  # a "document" with fewer words is an empty source row, not a resume


@dataclass
class Doc:
    doc_no: int
    raw: RawDoc
    markdown: str
    hash: str
    redactions: RedactionCounts
    provenance: list[dict] = field(default_factory=list)
    dup_group: int | None = None
    rate: int | None = None
    rate_source: str = "synthetic"

    @property
    def id(self) -> str:
        return doc_id(self.doc_no)

    @property
    def headline(self) -> str:
        first = self.markdown.split("\n", 1)[0]
        return first[2:].strip() if first.startswith("# ") else ""

    @property
    def words(self) -> int:
        return len(self.markdown.split())


def _load_source(src: Source) -> Iterator[RawDoc]:
    if not src.path.exists():
        if src.optional:
            return iter(())
        raise ResumesError("DATA_MISSING", f"source {src.id}: {src.path} does not exist (scripts/get-data.sh downloads the public datasets)")
    if src.loader == "csv_livecareer":
        return csv_livecareer.load(src.path, src.id)
    if src.loader == "csv_kaggle":
        return csv_kaggle.load(src.path, src.id)
    if src.loader == "markdown_dir":
        return markdown_dir.load(src.path, src.id)
    raise ValueError(f"source {src.id}: unknown loader {src.loader!r}")


def _front_matter(doc: Doc, currency: str) -> dict:
    meta = doc.raw.meta
    fm: dict = {
        "id": doc.id,
        "doc_no": doc.doc_no,
        "source": doc.raw.source,
        "source_id": doc.raw.source_id,
        "category": doc.raw.category,
        "hash": doc.hash,
        "words": doc.words,
        "rate": doc.rate,
        "currency": currency,
        "rate_source": doc.rate_source,
        "seniority_hint": meta.get("seniority") or seniority_hint(doc.headline),
        "dup_group": doc.dup_group,
        "fixture": bool(meta.get("fixture", False)),
        "converter": meta.get("converter"),
    }
    for k in PASS_THROUGH_META:
        if k in meta and k not in fm:
            fm[k] = meta[k]
    if doc.provenance:
        fm["also_seen_as"] = doc.provenance
    return fm


def build(cfg: Config, *, limit: int | None = None, log=print) -> dict:
    out_dir = cfg.corpus_out
    md_dir = out_dir / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    registry = Registry.load(out_dir / "registry.json")

    docs: list[Doc] = []
    by_hash: dict[str, Doc] = {}
    per_source: dict[str, dict] = {}
    redactions = RedactionCounts()

    for src in cfg.sources:
        stats = per_source.setdefault(
            src.id, {"rows": 0, "docs": 0, "exact_duplicates": 0, "fallback_converter": 0, "skipped_empty": 0}
        )
        for raw in _load_source(src):
            stats["rows"] += 1
            if raw.meta.get("converter") == "str":
                stats["fallback_converter"] += 1
            md = normalize(raw.markdown)
            md, counts = redact(md)
            if len(md.split()) < MIN_WORDS:
                stats["skipped_empty"] += 1
                log(f"    skipped {raw.key}: {len(md.split())} words")
                continue
            redactions.add(counts)
            h = content_hash(md)
            if h in by_hash:
                by_hash[h].provenance.append({"source": raw.source, "source_id": raw.source_id, "category": raw.category})
                stats["exact_duplicates"] += 1
                continue
            entry = registry.assign(raw.source, raw.source_id, raw.category, h)
            doc = Doc(doc_no=entry.doc_no, raw=raw, markdown=md, hash=h, redactions=counts)
            by_hash[h] = doc
            docs.append(doc)
            stats["docs"] += 1
            if limit and len(docs) >= limit:
                break
        log(
            f"  {src.id:12} rows {stats['rows']:5}  docs {stats['docs']:5}  exact dups {stats['exact_duplicates']:4}"
            f"  fallback {stats['fallback_converter']}  skipped {stats['skipped_empty']}"
        )
        if limit and len(docs) >= limit:
            break

    for doc in docs:
        registry.by_key[doc.raw.key].provenance = doc.provenance
    n_deleted = registry.mark_missing({doc.raw.key for doc in docs})
    if n_deleted:
        log(f"  {n_deleted} registry entries marked deleted (source row gone; doc_no stays reserved)")

    log(f"  near-duplicate grouping (MinHash, Jaccard ≥ {NEAR_DUP_THRESHOLD}) …")
    groups = near_duplicate_groups({d.doc_no: d.markdown for d in docs})
    for doc in docs:
        doc.dup_group = groups.get(doc.doc_no)

    for doc in docs:
        meta = doc.raw.meta
        if meta.get("rate") is not None:
            doc.rate = int(round(float(meta["rate"])))
            doc.rate_source = "document"
        else:
            doc.rate = synthetic_rate(doc.raw.category, doc.id, meta.get("seniority"), doc.headline)
            doc.rate_source = "synthetic"

    written: set[str] = set()
    for doc in docs:
        fm = _front_matter(doc, cfg.currency)
        text = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + doc.markdown
        path = md_dir / f"{doc.id}.md"
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        written.add(path.name)
    stale = [p for p in md_dir.glob("*.md") if p.name not in written]
    for p in stale:
        p.unlink()

    registry.save()

    words = [d.words for d in docs]
    quality = {
        "no_headline": sum(1 for d in docs if not d.headline),
        "under_50_words": sum(1 for d in docs if d.words < 50),
        "html_tag_leftovers": sum(1 for d in docs if _HTML_TAG.search(d.markdown)),
        "mojibake_leftovers": sum(1 for d in docs if _MOJIBAKE.search(d.markdown)),
        "no_h2_section": sum(1 for d in docs if "\n## " not in d.markdown),
    }
    stats = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "docs": len(docs),
        "sources": per_source,
        "near_duplicate_groups": len(set(groups.values())),
        "near_duplicate_docs": len(groups),
        "redactions": {"emails": redactions.emails, "urls": redactions.urls, "phones": redactions.phones},
        "words": {
            "p50": int(statistics.median(words)) if words else 0,
            "p90": int(sorted(words)[int(len(words) * 0.9)]) if words else 0,
            "max": max(words) if words else 0,
            "min": min(words) if words else 0,
        },
        "rate_source": {
            "document": sum(1 for d in docs if d.rate_source == "document"),
            "synthetic": sum(1 for d in docs if d.rate_source == "synthetic"),
        },
        "quality": quality,
        "stale_files_removed": len(stale),
        "registry_deleted": n_deleted,
    }
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    return stats
