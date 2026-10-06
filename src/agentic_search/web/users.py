"""The people of `resumes web`: `.resumes/users.json`, written by the operator's commands and by the app server's email
and phone check.

    {"users": {"dana@example.com": {"hash": "scrypt$14$8$1$<salt>$<hash>", "created": "…", "role": "admin"},
               "ana@example.com":  {"created": "…", "role": "member", "phone": "+40…", "phones": ["+40…"],
                                    "verified": {"email": "…", "phone": "…"}, "seen": "…", "via": "check"}}}

The hash is scrypt (N=2^14, r=8, p=1, 16-byte salt, 32 bytes out, base64url without padding) and must stay
byte-compatible with `crypto.scryptSync` in the app server. Only the operator's accounts have one; the check's members
have none. A record without a role is an admin. Every record and every field is kept on rewrite, since the app server
writes ones this module does not know. Both programs take `users.lock` around a read-modify-write.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..errors import ResumesError

FILE = "users.json"
LOCK = "users.lock"
USAGE = "usage.jsonl"
MIN_PASSWORD = 8
ROLES = ("admin", "member", "blocked")
LOCK_STALE = 10.0   # seconds: a lock older than this was left by a process that died
LOCK_WAIT = 5.0     # seconds


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def normalize_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not n or any(ch.isspace() for ch in n) or len(n) > 200:
        raise ResumesError("BAD_ARGUMENT", "the account name is one word, e.g. an email address")
    return n


def hash_password(password: str, *, salt: bytes | None = None, log_n: int = 14, r: int = 8, p: int = 1) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**log_n, r=r, p=p, dklen=32)
    return f"scrypt${log_n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    parts = (stored or "").split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        log_n, r, p = (int(x) for x in parts[1:4])
        want = _unb64(parts[5])
        got = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(parts[4]), n=2**log_n, r=r, p=p, dklen=len(want))
    except (ValueError, TypeError):
        return False
    return bool(want) and hmac.compare_digest(want, got)


def role_of(rec: dict) -> str:
    r = rec.get("role")
    return r if r in ROLES else "admin"


def load(path: Path) -> dict[str, dict]:
    """name → the whole record, password or not (a rewrite must not drop the check's members)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    users = raw.get("users") if isinstance(raw, dict) else None
    out: dict[str, dict] = {}
    if isinstance(users, dict):
        for name, rec in users.items():
            if isinstance(rec, dict):
                out[str(name).strip().lower()] = dict(rec)
    return out


def save(path: Path, users: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"users": users}, indent=1) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


@contextmanager
def locked(path: Path):
    """`users.lock` next to the file, for one read-modify-write; the app server takes the same lock."""
    lock = path.with_name(LOCK)
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise ResumesError("BUSY", f"{lock} is held by another program; try again (or delete it if nothing is running)") from None
            time.sleep(0.02)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add(path: Path, name: str, password: str, role: str | None = None) -> tuple[str, bool]:
    """→ (the name as stored, whether it was new). A new account is an admin unless `role` says otherwise."""
    key = normalize_name(name)
    if len(password or "") < MIN_PASSWORD:
        raise ResumesError("BAD_ARGUMENT", f"the password is at least {MIN_PASSWORD} characters")
    if role is not None and role not in ROLES:
        raise ResumesError("BAD_ARGUMENT", f"the role is one of {', '.join(ROLES)}")
    digest = hash_password(password)
    with locked(path):
        users = load(path)
        new = key not in users
        rec = dict(users.get(key, {}))
        rec["hash"] = digest
        rec["created"] = rec.get("created") or _now()
        if role is not None:
            rec["role"] = role
        elif new:
            rec["role"] = "admin"
        if new:
            rec.setdefault("via", "operator")
        users[key] = rec
        save(path, users)
    return key, new


def remove(path: Path, name: str) -> bool:
    key = normalize_name(name)
    with locked(path):
        users = load(path)
        if key not in users:
            return False
        del users[key]
        save(path, users)
    return True


def block(path: Path, name: str) -> bool:
    """Kept as `blocked` rather than deleted, so neither its email nor any of its numbers passes the check again."""
    key = normalize_name(name)
    with locked(path):
        users = load(path)
        if key not in users:
            return False
        users[key] = {**users[key], "role": "blocked", "blocked": _now()}
        save(path, users)
    return True


def names(path: Path) -> list[tuple[str, str | None]]:
    return sorted((n, rec.get("created")) for n, rec in load(path).items())


def records(path: Path) -> list[tuple[str, dict]]:
    return sorted(load(path).items())


def usage(path: Path, now: datetime | None = None) -> dict[str, dict]:
    """`usage.jsonl` (one line per assistant turn) → name → {day_turns, day_cost, month_turns, month_cost}."""
    now = now or datetime.now(timezone.utc)
    day, month = now - timedelta(days=1), now - timedelta(days=30)
    out: dict[str, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
            at = datetime.fromtimestamp(float(row["at"]) / 1000, timezone.utc)
            user = str(row["user"])
            cost = float(row.get("cost") or 0)
        except (ValueError, KeyError, TypeError):
            continue
        if at < month:
            continue
        u = out.setdefault(user, {"day_turns": 0, "day_cost": 0.0, "month_turns": 0, "month_cost": 0.0})
        u["month_turns"] += 1
        u["month_cost"] += cost
        if at >= day:
            u["day_turns"] += 1
            u["day_cost"] += cost
    return out
