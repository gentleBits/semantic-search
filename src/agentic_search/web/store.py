"""The web facts of one conversation, kept next to the engine's session files.

    .resumes/sessions/web-<date>-<time>-<hex>/
      session.json, rs_NN.json, filters/, …     the engine's (session/store.py)
      web.json                                   title, undo trail, last change of the count, pending ranking criterion
      chat.jsonl                                 the transcript, append-only
      jd-1.md                                    a pasted or attached job description

A conversation is an engine session whose directory holds `web.json`; other sessions are not listed by the UI.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path

from ..errors import ResumesError
from ..session.store import now_iso, validate_session_id, write_atomic

WEB_FILE = "web.json"
CHAT_FILE = "chat.jsonl"
TITLE_MAX = 48
PREFIX = "web-"


def new_id() -> str:
    return PREFIX + datetime.now().strftime("%Y-%m-%d-%H%M") + "-" + secrets.token_hex(2)


class WebSession:
    def __init__(self, dir: Path) -> None:
        self.dir = dir
        self.id = dir.name
        self._meta: dict | None = None
        self._chat: list[dict] | None = None

    # ------------------------------------------------------------ files
    @property
    def path(self) -> Path:
        return self.dir / WEB_FILE

    @property
    def chat_path(self) -> Path:
        return self.dir / CHAT_FILE

    def exists(self) -> bool:
        return self.path.is_file()

    @classmethod
    def create(cls, base: Path, sid: str | None = None) -> "WebSession":
        sid = validate_session_id(sid) if sid else new_id()
        w = cls(base / sid)
        if w.exists():
            raise ResumesError("SESSION_EXISTS", sid)
        w._meta = {"title": None, "created_at": now_iso(), "updated_at": now_iso(), "prev": {}, "last": None, "pending": None,
                   "judge_context": {}, "jd": 0, "next_message": 1}
        w._chat = []
        w.save()
        return w

    @classmethod
    def open(cls, base: Path, sid: str) -> "WebSession":
        w = cls(base / validate_session_id(sid))
        if not w.exists():
            raise ResumesError("UNKNOWN_SESSION", sid)
        return w

    # ------------------------------------------------------------ meta
    @property
    def meta(self) -> dict:
        if self._meta is None:
            try:
                self._meta = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise ResumesError("UNKNOWN_SESSION", self.id) from None
        return self._meta

    def save(self) -> None:
        self.meta["updated_at"] = now_iso()
        write_atomic(self.path, json.dumps(self.meta, ensure_ascii=False, indent=1))

    @property
    def title(self) -> str | None:
        return self.meta.get("title")

    def name_from(self, text: str) -> None:
        """Name the conversation after its first question."""
        if self.meta.get("title"):
            return
        t = " ".join(text.split()).strip(" ?!.")
        if not t or t.startswith("/"):
            return
        t = re.sub(r"^(who|which people|which|show me|find|list)\s+(has |have |is |are |the |me |worked on |works on |knows |know )?", "", t, flags=re.I) or t
        t = t[:1].upper() + t[1:]
        self.meta["title"] = t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1].rstrip() + "…"
        self.save()

    # ------------------------------------------------------------ the undo trail and the last change of the count
    def stepped(self, before: tuple[str, int] | None, after: tuple[str, int], *, made: bool) -> None:
        """`made`: this step created `after`, so undo leads back to `before`; a jump to an existing set
        leaves the undo trail alone."""
        if before is None or before[0] == after[0]:
            return
        if made:
            self.meta.setdefault("prev", {})[after[0]] = before[0]
        self.meta["last"] = {"set": after[0], "from": before[1], "to": after[1]} if before[1] != after[1] else None
        self.save()

    def prev_of(self, rs_id: str) -> str | None:
        return self.meta.get("prev", {}).get(rs_id)

    def delta_of(self, rs_id: str) -> list[int] | None:
        """What the last step did to the count (`165 → 25`), while its set is the one shown."""
        last = self.meta.get("last")
        return [last["from"], last["to"]] if last and last.get("set") == rs_id else None

    # ------------------------------------------------------------ the ranking that waits for a smaller set
    @property
    def pending(self) -> dict | None:
        return self.meta.get("pending")

    def set_pending(self, criterion: str | None) -> None:
        self.meta["pending"] = {"criterion": criterion, "at": now_iso()} if criterion else None
        self.save()

    # ------------------------------------------------------------ job descriptions
    def save_jd(self, text: str, name: str | None = None) -> str:
        n = int(self.meta.get("jd", 0)) + 1
        self.meta["jd"] = n
        fname = f"jd-{n}.md"
        write_atomic(self.dir / fname, text if text.endswith("\n") else text + "\n")      # unchanged: the embedding cache is keyed by the text
        self.meta.setdefault("jd_names", {})[fname] = (name or "").strip() or jd_title(text)
        self.save()
        return fname

    def jd_name(self, fname: str) -> str:
        return self.meta.get("jd_names", {}).get(Path(fname).name) or Path(fname).name

    # ------------------------------------------------------------ chat
    @property
    def chat(self) -> list[dict]:
        if self._chat is None:
            self._chat = []
            if self.chat_path.is_file():
                for line in self.chat_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            self._chat.append(json.loads(line))
                        except ValueError:
                            continue
        return self._chat

    def append(self, message: dict) -> dict:
        n = int(self.meta.get("next_message", 1))
        self.meta["next_message"] = n + 1
        m = {"id": f"m{n}", "ts": now_iso(), **message}
        self.chat.append(m)
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.chat_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
        self.save()
        return m


def jd_title(text: str) -> str:
    """A job description's first meaningful line."""
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip(" *_-")
        if len(line) >= 3:
            return line if len(line) <= 40 else line[:39].rstrip() + "…"
    return "job description"


def list_web_sessions(base: Path) -> list[dict]:
    out = []
    if not base.is_dir():
        return out
    for d in base.iterdir():
        w = WebSession(d)
        if not w.exists():
            continue
        try:
            meta = w.meta
        except (ResumesError, ValueError):
            continue
        current = None
        try:
            current = json.loads((d / "session.json").read_text(encoding="utf-8")).get("current")
            count = json.loads((d / f"{current}.json").read_text(encoding="utf-8")).get("count") if current else None
        except (OSError, ValueError):
            count = None
        out.append({"id": w.id, "title": meta.get("title"), "created_at": meta.get("created_at"), "updated_at": meta.get("updated_at"),
                    "messages": max(0, int(meta.get("next_message", 1)) - 1), "count": count})
    return sorted(out, key=lambda r: r.get("updated_at") or "", reverse=True)
