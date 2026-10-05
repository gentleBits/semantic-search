"""The accounts of `resumes web`: the scrypt hash, users.json and the `resumes users` command."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_search import cli
from agentic_search.errors import ResumesError
from agentic_search.web import users

# salt 00 01 … 0f; the app server's test holds the same line, so Python and Node must hash alike
KNOWN = "scrypt$14$8$1$AAECAwQFBgcICQoLDA0ODw$79MuiuHQMHk6N9iL1_is2-Bl4yCPwYO0SwBG-PNDijs"


def test_the_hash_and_its_check():
    assert users.hash_password("cretzuel", salt=bytes(range(16))) == KNOWN
    assert users.verify_password("cretzuel", KNOWN)
    assert not users.verify_password("cretzuel ", KNOWN)
    assert not users.verify_password("", KNOWN)
    fresh = users.hash_password("cretzuel")
    assert fresh.startswith("scrypt$14$8$1$") and fresh != KNOWN and users.verify_password("cretzuel", fresh)
    for broken in ("", "md5$x", "scrypt$14$8$1$AA", "scrypt$x$8$1$AA$AA"):
        assert not users.verify_password("cretzuel", broken)


def test_the_file(tmp_path: Path):
    path = tmp_path / "users.json"
    assert users.names(path) == []
    assert users.add(path, " Dana@Example.com ", "cretzuel") == ("dana@example.com", True)
    assert users.add(path, "cole@example.com", "cretzuel") == ("cole@example.com", True)
    assert path.stat().st_mode & 0o777 == 0o600
    raw = json.loads(path.read_text())
    assert set(raw["users"]) == {"dana@example.com", "cole@example.com"}
    assert raw["users"]["dana@example.com"]["hash"].startswith("scrypt$14$8$1$") and raw["users"]["dana@example.com"]["created"]
    created = raw["users"]["dana@example.com"]["created"]
    assert users.add(path, "dana@example.com", "another-one") == ("dana@example.com", False), "a known name: the password changes"
    again = json.loads(path.read_text())["users"]["dana@example.com"]
    assert users.verify_password("another-one", again["hash"]) and again["created"] == created
    assert [n for n, _ in users.names(path)] == ["cole@example.com", "dana@example.com"]
    assert users.remove(path, "COLE@example.com") and not users.remove(path, "cole@example.com")
    assert [n for n, _ in users.names(path)] == ["dana@example.com"]
    with pytest.raises(ResumesError, match="at least 8"):
        users.add(path, "x@y", "short")
    for bad in ("", "  ", "two words", "a" * 201):
        with pytest.raises(ResumesError, match="one word"):
            users.add(path, bad, "cretzuel")
    path.write_text("not json")
    assert users.names(path) == [], "a broken file is an empty one, not a crash"


def test_the_command(tmp_path: Path, monkeypatch, capsys):
    cfg = cli._cfg()
    monkeypatch.setattr(cli, "_cfg", lambda: replace(cfg, web=replace(cfg.web, state_dir=tmp_path / "state")))
    assert cli.main(["users", "add", "dana@example.com", "--password", "cretzuel"]) == 0
    assert "added dana@example.com (admin) · 1 user · login is on" in capsys.readouterr().err
    assert cli.main(["users", "add", "cole@example.com", "--password", "cretzuel"]) == 0
    assert cli.main(["users", "list"]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines()[0].startswith(f"cole@example.com\t{datetime.now(timezone.utc).year}-") and "2 users" in out.err
    assert (tmp_path / "state" / "users.json").stat().st_mode & 0o777 == 0o600
    assert cli.main(["users", "add", "x@y", "--password", "short"]) == 1
    assert "at least 8" in capsys.readouterr().err
    assert cli.main(["users", "remove", "nobody"]) == 1
    assert cli.main(["users", "remove", "cole@example.com"]) == 0
    assert "1 user left · login is on" in capsys.readouterr().err
    assert cli.main(["users", "remove", "dana@example.com"]) == 0
    assert "0 users left · login is off" in capsys.readouterr().err
