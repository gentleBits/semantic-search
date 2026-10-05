from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawDoc:
    """One document as a loader yields it: already markdown, not yet normalised."""

    source: str
    source_id: str
    category: str | None
    markdown: str
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"
