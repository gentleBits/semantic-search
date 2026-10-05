"""Stable document numbers.

`corpus/registry.json` maps a source row (`source:source_id`) to a `doc_no`
that is assigned on first sight and never changes or gets reused, even if the
converter changes and the content hash moves. Exact duplicates found in a
build do not get a doc_no; they are recorded as provenance of the first copy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REGISTRY_VERSION = 1


def doc_id(doc_no: int) -> str:
    return f"r{doc_no:06d}"


@dataclass
class Entry:
    doc_no: int
    source: str
    source_id: str
    category: str | None
    hash: str
    first_seen: str
    provenance: list[dict] = field(default_factory=list)
    deleted: bool = False   # the source row is gone; the doc_no stays reserved forever

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.next_doc_no = 1
        self.by_key: dict[str, Entry] = {}

    @classmethod
    def load(cls, path: Path) -> "Registry":
        reg = cls(path)
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            reg.next_doc_no = int(data["next_doc_no"])
            for d in data["docs"]:
                e = Entry(**d)
                reg.by_key[e.key] = e
        return reg

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": REGISTRY_VERSION,
            "next_doc_no": self.next_doc_no,
            "docs": [asdict(e) for e in sorted(self.by_key.values(), key=lambda e: e.doc_no)],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def assign(self, source: str, source_id: str, category: str | None, content_hash: str) -> Entry:
        """Return the entry for this source row, creating a new doc_no if unseen."""
        key = f"{source}:{source_id}"
        e = self.by_key.get(key)
        if e is None:
            e = Entry(
                doc_no=self.next_doc_no,
                source=source,
                source_id=source_id,
                category=category,
                hash=content_hash,
                first_seen=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self.next_doc_no += 1
            self.by_key[key] = e
        else:
            e.hash = content_hash
            e.category = category
            e.provenance = []
            e.deleted = False
        return e

    def mark_missing(self, seen_keys: set[str]) -> int:
        """Entries whose source row was not seen in this build are marked deleted (never removed)."""
        n = 0
        for key, e in self.by_key.items():
            if key not in seen_keys and not e.deleted:
                e.deleted = True
                n += 1
        return n

    @property
    def live(self) -> list[Entry]:
        return [e for e in self.by_key.values() if not e.deleted]
