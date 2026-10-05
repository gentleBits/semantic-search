"""Sessions and result sets.

    .resumes/sessions/CURRENT_SESSION         the session id used when $RESUMES_SESSION is unset
    .resumes/sessions/<sid>/session.json      {"current": "rs_03", "index_version": …, "created_at": …, "next_set": 4, "next_judgment": 2}
    .resumes/sessions/<sid>/rs_NN.json        one result set; immutable except `cursor`
    .resumes/sessions/<sid>/judgments.parquet written by judgments.py
    .resumes/sessions/<sid>/scores.json       where the agent writes scores; scores/j_NN.json keeps every accepted file
    .resumes/sessions/<sid>/filters/fN.json   one saved filter: who matches it, computed once (filters.py)
    .resumes/sessions/<sid>/filters/index.json  condition → filter id: the same condition is the same filter
    .resumes/sessions/<sid>/views.json        combination of filters (+ sort + ranking) → the set that already holds it

A result set's `order` is authoritative. The `bitmap` field is derived from it on every write and
never trusted on read, so the two cannot disagree. A set also records `filters`, the ids of the
filters it is the intersection of; older sets and union/minus sets have none.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pyroaring import BitMap

from ..errors import ResumesError
from .bitmap import encode

ENV_VAR = "RESUMES_SESSION"
CURRENT_FILE = "CURRENT_SESSION"
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SET_ID_RE = re.compile(r"(?:rs_?)?(\d+)")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_id(n: int) -> str:
    return f"rs_{n:02d}"


def judgment_id(n: int) -> str:
    return f"j_{n:02d}"


def filter_id(n: int) -> str:
    return f"f{n}"


def filter_number(fid: str) -> int:
    return int(fid[1:])


def set_number(rs_id: str) -> int:
    return int(rs_id.split("_", 1)[1])


def parse_set_id(text: str | None) -> str | None:
    """'rs_03' / 'rs_3' / '3' → 'rs_03'. None / 'current' / '.' → None, meaning the session's current set."""
    if text is None:
        return None
    t = text.strip().lower()
    if t in ("", "current", "."):
        return None
    m = SET_ID_RE.fullmatch(t)
    if not m:
        raise ResumesError("UNKNOWN_SET", f"{text}: not a set id (expected rs_NN)")
    return set_id(int(m.group(1)))


def validate_session_id(sid: str) -> str:
    if not SESSION_ID_RE.fullmatch(sid):
        raise ResumesError("BAD_SESSION_ID", f"{sid!r}: use letters, digits, '.', '_' or '-' (max 128)")
    return sid


def new_session_id() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M") + "-" + secrets.token_hex(2)


@dataclass
class ResultSet:
    id: str
    parent: str | None
    op: str
    args: dict
    index_version: str
    count: int
    order: list[int]
    judgment: str | None = None
    sort: list[str] = field(default_factory=list)
    cursor: int = 0
    page_size: int = 10
    created_at: str = ""
    filters: list[str] | None = None      # ids of the active filters; None for older sets and union/minus sets
    ranking_off: bool = False             # `drop rank` / `clear`: no judgment is shown until the next `sort --by judgment`

    def bitmap(self) -> BitMap:
        return BitMap(self.order)

    def to_json(self) -> str:
        d = asdict(self)
        d["bitmap"] = encode(self.bitmap())
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "ResultSet":
        d = json.loads(text)
        d.pop("bitmap", None)          # derived from `order`; rebuilt on the next write
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def pages(self) -> int:
        return max(1, -(-self.count // self.page_size)) if self.count else 0

    @property
    def page_no(self) -> int:
        """Number of the page that ends at the cursor (0 when nothing was shown yet)."""
        return -(-self.cursor // self.page_size) if self.cursor else 0


class Session:
    def __init__(self, dir: Path) -> None:
        self.dir = dir
        self.id = dir.name
        self._meta: dict | None = None

    # ------------------------------------------------------------ files
    @property
    def path(self) -> Path:
        return self.dir / "session.json"

    @property
    def scores_path(self) -> Path:
        return self.dir / "scores.json"

    @property
    def scores_dir(self) -> Path:
        return self.dir / "scores"

    @property
    def judgments_path(self) -> Path:
        return self.dir / "judgments.parquet"

    @property
    def filters_dir(self) -> Path:
        return self.dir / "filters"

    @property
    def filter_index_path(self) -> Path:
        return self.filters_dir / "index.json"

    @property
    def views_path(self) -> Path:
        return self.dir / "views.json"

    def filter_path(self, fid: str) -> Path:
        return self.filters_dir / f"{fid}.json"

    def set_path(self, rs_id: str) -> Path:
        return self.dir / f"{rs_id}.json"

    def exists(self) -> bool:
        return self.path.is_file()

    @classmethod
    def create(cls, dir: Path, index_version: str) -> "Session":
        s = cls(dir)
        s._meta = {"current": None, "index_version": index_version, "created_at": now_iso(), "next_set": 1, "next_judgment": 1}
        s.save_meta()
        return s

    # ------------------------------------------------------------ meta
    @property
    def meta(self) -> dict:
        if self._meta is None:
            try:
                self._meta = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise ResumesError("NO_SESSION", f"{self.id}: no session.json (run `resumes search …` first)") from None
        return self._meta

    def save_meta(self) -> None:
        write_atomic(self.path, json.dumps(self.meta, ensure_ascii=False, indent=1))

    # ------------------------------------------------------------ sets
    def set_ids(self) -> list[str]:
        ids = [p.stem for p in self.dir.glob("rs_*.json")]
        return sorted(ids, key=set_number)

    def get(self, rs_id: str) -> ResultSet:
        p = self.set_path(rs_id)
        if not p.is_file():
            have = self.set_ids()
            span = f"{have[0]}..{have[-1]}" if len(have) > 1 else (have[0] if have else "none")
            raise ResumesError("UNKNOWN_SET", f"{rs_id} (have {span})")
        return ResultSet.from_json(p.read_text(encoding="utf-8"))

    def current(self) -> ResultSet | None:
        cur = self.meta.get("current")
        return self.get(cur) if cur else None

    def resolve(self, set_arg: str | None) -> ResultSet:
        """A set argument from the command line, or the current set when it is None/'current'."""
        rs_id = parse_set_id(set_arg)
        if rs_id is None:
            rs = self.current()
            if rs is None:
                raise ResumesError("NO_CURRENT_SET", "run `resumes search …` first")
            return rs
        return self.get(rs_id)

    def set_current(self, rs_id: str) -> None:
        self.meta["current"] = rs_id
        self.save_meta()

    def new_set(self, *, parent: str | None, op: str, args: dict, index_version: str, order: list[int],
                judgment: str | None = None, sort: list[str] | None = None, page_size: int = 10,
                filters: list[str] | None = None, ranking_off: bool = False) -> ResultSet:
        rs = ResultSet(
            id=set_id(int(self.meta["next_set"])), parent=parent, op=op, args=args, index_version=index_version,
            count=len(order), order=list(order), judgment=judgment, sort=list(sort or []), cursor=0,
            page_size=page_size, created_at=now_iso(), filters=None if filters is None else list(filters), ranking_off=ranking_off,
        )
        write_atomic(self.set_path(rs.id), rs.to_json())
        self.meta["next_set"] = int(self.meta["next_set"]) + 1
        self.meta["current"] = rs.id
        self.save_meta()
        return rs

    def save_cursor(self, rs: ResultSet, cursor: int, page_size: int | None = None) -> None:
        rs.cursor = max(0, min(cursor, rs.count))
        if page_size:
            rs.page_size = page_size
        write_atomic(self.set_path(rs.id), rs.to_json())

    def lineage(self, rs_id: str) -> list[str]:
        """The set and its ancestors, nearest first."""
        out: list[str] = []
        seen: set[str] = set()
        cur: str | None = rs_id
        while cur and cur not in seen:
            out.append(cur)
            seen.add(cur)
            p = self.set_path(cur)
            if not p.is_file():
                break
            cur = ResultSet.from_json(p.read_text(encoding="utf-8")).parent
        return out

    def all_sets(self) -> list[ResultSet]:
        return [self.get(i) for i in self.set_ids()]

    def next_judgment_id(self) -> str:
        jid = judgment_id(int(self.meta["next_judgment"]))
        self.meta["next_judgment"] = int(self.meta["next_judgment"]) + 1
        self.save_meta()
        return jid

    # ------------------------------------------------------------ saved filters and views
    def next_filter_id(self) -> str:
        n = int(self.meta.get("next_filter", 1))          # older sessions have no counter yet
        self.meta["next_filter"] = n + 1
        self.save_meta()
        return filter_id(n)

    def filter_ids(self) -> list[str]:
        ids = [p.stem for p in self.filters_dir.glob("f*.json") if p.stem[1:].isdigit()]
        return sorted(ids, key=filter_number)

    def _read_map(self, path: Path) -> dict[str, str]:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def filter_index(self) -> dict[str, str]:
        """condition key → filter id."""
        return self._read_map(self.filter_index_path)

    def remember_filter(self, key: str, fid: str) -> None:
        d = self.filter_index()
        d[key] = fid
        write_atomic(self.filter_index_path, json.dumps(d, ensure_ascii=False, indent=1))

    def views(self) -> dict[str, str]:
        """view key (filters + sort + ranking revision + index version) → id of the set that holds that view."""
        return self._read_map(self.views_path)

    def remember_view(self, key: str, rs_id: str) -> None:
        d = self.views()
        d[key] = rs_id
        write_atomic(self.views_path, json.dumps(d, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- session identity


def current_session_id(base: Path, explicit: str | None = None) -> tuple[str | None, str]:
    """(session id, where it came from): 'arg' | 'env' | 'file' | 'none'."""
    if explicit:
        return validate_session_id(explicit), "arg"
    env = os.environ.get(ENV_VAR)
    if env:
        return validate_session_id(env), "env"
    f = base / CURRENT_FILE
    if f.is_file():
        sid = f.read_text(encoding="utf-8").strip()
        if sid:
            return validate_session_id(sid), "file"
    return None, "none"


def write_current_session(base: Path, sid: str) -> None:
    write_atomic(base / CURRENT_FILE, sid + "\n")


def open_session(base: Path, index_version: str, *, create: bool, explicit: str | None = None) -> Session | None:
    """The active session under `base` (the sessions directory); created on demand when `create` is set (the first `search`)."""
    sid, source = current_session_id(base, explicit)
    if sid is None:
        if not create:
            return None
        sid = new_session_id()
        s = Session.create(base / sid, index_version)
        write_current_session(base, sid)
        return s
    s = Session(base / sid)
    if s.exists():
        return s
    if not create:
        return None
    return Session.create(s.dir, index_version)


def new_session(base: Path, index_version: str, sid: str | None = None, *, make_current: bool = True) -> Session:
    """A fresh session. `make_current=False` for a process that is itself the binding (the MCP server), so the
    directory's CURRENT_SESSION, which other shells rely on, is left alone."""
    sid = validate_session_id(sid) if sid else new_session_id()
    s = Session(base / sid)
    if s.exists():
        raise ResumesError("SESSION_EXISTS", sid)
    s = Session.create(s.dir, index_version)
    if make_current:
        write_current_session(base, sid)
    return s


def use_session(base: Path, sid: str) -> Session:
    s = Session(base / validate_session_id(sid))
    if not s.exists():
        raise ResumesError("UNKNOWN_SESSION", f"{sid} (see `resumes session list`)")
    write_current_session(base, sid)
    return s


def list_sessions(base: Path) -> list[dict]:
    out = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        s = Session(d)
        if not s.exists():
            continue
        try:
            meta = s.meta
        except ResumesError:
            continue
        out.append({
            "id": s.id, "created_at": meta.get("created_at"), "sets": len(s.set_ids()), "current": meta.get("current"),
            "judgments": max(0, int(meta.get("next_judgment", 1)) - 1),
            "updated_at": datetime.fromtimestamp(s.path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        })
    return out


def gc_sessions(base: Path, days: int, keep: str | None = None) -> list[str]:
    """Delete sessions untouched for more than `days` days. Never the active one."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = []
    if not base.is_dir():
        return removed
    for d in sorted(base.iterdir()):
        s = Session(d)
        if not s.exists() or s.id == keep:
            continue
        newest = max(p.stat().st_mtime for p in d.rglob("*") if p.is_file())
        if datetime.fromtimestamp(newest, tz=timezone.utc) < cutoff:
            shutil.rmtree(d)
            removed.append(s.id)
    return removed
