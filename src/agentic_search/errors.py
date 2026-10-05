"""One error type for the query path. The CLI prints `CODE message`; `data` goes to --json."""

from __future__ import annotations


class ResumesError(Exception):
    def __init__(self, code: str, message: str = "", data: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.data = data or {}
        super().__init__(f"{code} {message}".strip())

    def __str__(self) -> str:
        return f"{self.code} {self.message}".strip()
