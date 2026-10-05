"""`resumes corpus build --only-changed`: convert only the inbox files the registry has not seen."""

from __future__ import annotations

import yaml

from ..config import Config
from .corpus import MIN_WORDS, Doc, _front_matter, _load_source
from .dedupe import content_hash
from .normalize import normalize
from .rates import synthetic_rate
from .redact import redact
from .registry import Registry


def add_new(cfg: Config, log=print) -> list[str]:
    """New inbox documents → corpus/md; returns their ids. No near-duplicate pass: dup_group waits for the next full build."""
    out_dir = cfg.corpus_out
    md_dir = out_dir / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    registry = Registry.load(out_dir / "registry.json")
    known_hashes = {e.hash for e in registry.by_key.values()}
    added: list[str] = []
    for src in cfg.sources:
        if src.loader != "markdown_dir" or not src.path.is_dir():
            continue
        for raw in _load_source(src):
            if raw.key in registry.by_key:
                continue
            md, counts = redact(normalize(raw.markdown))
            if len(md.split()) < MIN_WORDS:
                log(f"    skipped {raw.key}: {len(md.split())} words")
                continue
            h = content_hash(md)
            if h in known_hashes:
                log(f"    skipped {raw.key}: exact duplicate of an indexed document")
                continue
            entry = registry.assign(raw.source, raw.source_id, raw.category, h)
            doc = Doc(doc_no=entry.doc_no, raw=raw, markdown=md, hash=h, redactions=counts)
            meta = raw.meta
            if meta.get("rate") is not None:
                doc.rate, doc.rate_source = int(round(float(meta["rate"]))), "document"
            else:
                doc.rate, doc.rate_source = synthetic_rate(raw.category, doc.id, meta.get("seniority"), doc.headline), "synthetic"
            fm = _front_matter(doc, cfg.currency)
            (md_dir / f"{doc.id}.md").write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + doc.markdown, encoding="utf-8")
            known_hashes.add(h)
            added.append(doc.id)
            log(f"    added {doc.id} from {raw.key}")
    if added:
        registry.save()
    return added
