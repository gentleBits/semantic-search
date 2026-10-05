"""The users file shared with the app server (fields, lock, roles, block, usage) and the [signup]/[limits] policy."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_search import cli, config
from agentic_search.errors import ResumesError
from agentic_search.web import users


def member(path: Path, name: str, phone: str) -> None:
    """A member as the app server's sign-up writes one."""
    raw = json.loads(path.read_text()) if path.exists() else {"users": {}}
    raw["users"][name] = {"hash": users.hash_password("long enough pw"), "created": "2026-10-01T10:00:00+00:00", "role": "member",
                          "phone": phone, "verified": {"email": "2026-10-01T09:58:00+00:00", "phone": "2026-10-01T09:59:00+00:00"},
                          "via": "signup", "something_new": [1, 2]}
    path.write_text(json.dumps(raw))


def test_every_field_survives_the_operators_commands(tmp_path: Path):
    path = tmp_path / "users.json"
    users.add(path, "dana@example.com", "cretzuel")
    member(path, "ana@example.com", "+40712345678")
    before = json.loads(path.read_text())["users"]["ana@example.com"]
    users.add(path, "cole@example.com", "cretzuel")                  # another name
    users.add(path, "dana@example.com", "another-one")                # a password change
    users.remove(path, "cole@example.com")
    after = json.loads(path.read_text())["users"]
    assert after["ana@example.com"] == before, "a member's phone, role, verified and unknown fields are kept"
    assert after["dana@example.com"]["role"] == "admin" and after["dana@example.com"]["via"] == "operator"
    assert users.add(path, "ana@example.com", "new password!") == ("ana@example.com", False)
    ana = json.loads(path.read_text())["users"]["ana@example.com"]
    assert ana["role"] == "member" and ana["phone"] == "+40712345678", "a password change keeps the role and the phone"
    assert users.verify_password("new password!", ana["hash"])
    users.add(path, "ana@example.com", "new password!", role="admin")
    assert users.role_of(json.loads(path.read_text())["users"]["ana@example.com"]) == "admin"
    with pytest.raises(ResumesError, match="role"):
        users.add(path, "x@y", "cretzuel", role="owner")


def test_an_account_without_a_role_is_an_admin(tmp_path: Path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"users": {"dana@example.com": {"hash": users.hash_password("cretzuel"), "created": "2026-09-30T18:00:00+00:00"}}}))
    assert users.role_of(users.load(path)["dana@example.com"]) == "admin"
    users.add(path, "dana@example.com", "cretzuel2")
    assert "role" not in json.loads(path.read_text())["users"]["dana@example.com"], "a password change does not write a role it was not told"


def test_block(tmp_path: Path):
    path = tmp_path / "users.json"
    member(path, "ana@example.com", "+40712345678")
    assert users.block(path, "ANA@example.com")
    rec = json.loads(path.read_text())["users"]["ana@example.com"]
    assert rec["role"] == "blocked" and rec["phone"] == "+40712345678" and rec["blocked"], "kept, with its phone: both stay taken"
    assert not users.block(path, "nobody@example.com")
    users.add(path, "ana@example.com", "new password!", role="member")
    assert users.role_of(json.loads(path.read_text())["users"]["ana@example.com"]) == "member", "let back in"


def test_the_lock(tmp_path: Path):
    path = tmp_path / "users.json"
    lock = tmp_path / users.LOCK
    users.add(path, "a@x", "cretzuel")
    assert not lock.exists(), "released after the write"
    # held by someone else for a moment: the write waits for it, then goes through
    lock.write_text("4242\n")
    threading.Timer(0.3, lambda: lock.unlink()).start()
    t0 = time.monotonic()
    users.add(path, "b@x", "cretzuel")
    assert time.monotonic() - t0 >= 0.25 and "b@x" in users.load(path)
    # left by a process that died: taken over
    lock.write_text("4242\n")
    old = time.time() - users.LOCK_STALE - 5
    os.utime(lock, (old, old))
    users.add(path, "c@x", "cretzuel")
    assert set(users.load(path)) == {"a@x", "b@x", "c@x"} and not lock.exists()
    # held and fresh for longer than the wait: refused, nothing written
    lock.write_text("4242\n")
    orig = users.LOCK_WAIT
    users.LOCK_WAIT = 0.2
    try:
        with pytest.raises(ResumesError, match="held by another program"):
            users.add(path, "d@x", "cretzuel")
    finally:
        users.LOCK_WAIT = orig
        lock.unlink()
    assert "d@x" not in users.load(path)


def test_the_lock_against_a_concurrent_writer(tmp_path: Path):
    """Twenty writers at once (threads here, the app server's process in production): no account is lost."""
    path = tmp_path / "users.json"
    errors: list[Exception] = []

    def one(i: int) -> None:
        try:
            users.add(path, f"u{i}@x", "cretzuel")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors and len(users.load(path)) == 20


def test_usage(tmp_path: Path):
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    ms = lambda h: int((now.timestamp() - h * 3600) * 1000)  # noqa: E731
    lines = [{"at": ms(1), "user": "ana@example.com", "sid": "s1", "cost": 0.004, "tokens": 900, "model": "openai:gpt-5-mini"},
             {"at": ms(2), "user": "ana@example.com", "sid": "s1", "cost": 0.006, "tokens": 900, "model": "openai:gpt-5-mini"},
             {"at": ms(30), "user": "ana@example.com", "sid": "s2", "cost": 0.5, "tokens": 9000, "model": "openai:gpt-5"},
             {"at": ms(24 * 40), "user": "ana@example.com", "sid": "s0", "cost": 9.0, "tokens": 1, "model": "x"},
             {"at": ms(3), "user": "dana@example.com", "sid": "s3", "cost": 0.01, "tokens": 1, "model": "x"}]
    (tmp_path / "usage.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    u = users.usage(tmp_path / "usage.jsonl", now=now)
    assert u["ana@example.com"]["day_turns"] == 2 and round(u["ana@example.com"]["day_cost"], 4) == 0.01
    assert u["ana@example.com"]["month_turns"] == 3 and round(u["ana@example.com"]["month_cost"], 4) == 0.51, "40 days ago is not counted"
    assert u["dana@example.com"]["day_turns"] == 1


def test_the_commands(tmp_path: Path, monkeypatch, capsys):
    cfg = cli._cfg()
    state = tmp_path / "state"
    monkeypatch.setattr(cli, "_cfg", lambda: replace(cfg, web=replace(cfg.web, state_dir=state)))
    assert cli.main(["users", "add", "dana@example.com", "--password", "cretzuel"]) == 0
    assert "added dana@example.com (admin)" in capsys.readouterr().err
    assert cli.main(["users", "add", "eve@example.com", "--password", "cretzuel", "--member"]) == 0
    assert "(member)" in capsys.readouterr().err
    member(state / "users.json", "ana@example.com", "+40712345678")
    assert cli.main(["users", "list"]) == 0
    rows = [r.split("\t") for r in capsys.readouterr().out.splitlines()]
    assert rows[0][0] == "ana@example.com" and rows[0][2:] == ["member", "+40712345678", "signup"]
    assert rows[1][0] == "dana@example.com" and rows[1][2:] == ["admin", "-", "operator"]
    assert cli.main(["users", "block", "ana@example.com"]) == 0
    assert "blocked ana@example.com" in capsys.readouterr().err
    assert cli.main(["users", "block", "nobody@x"]) == 1
    (state / "usage.jsonl").write_text(json.dumps({"at": int(time.time() * 1000), "user": "eve@example.com", "sid": "s", "cost": 0.0123, "tokens": 5, "model": "m"}) + "\n")
    assert cli.main(["users", "usage"]) == 0
    out = capsys.readouterr()
    assert "eve@example.com\tmember\t1\t$0.0123\t1\t$0.0123" in out.out and "1 person used the assistant" in out.err
    with pytest.raises(SystemExit):
        cli.main(["users", "add", "x@y", "--member", "--admin", "--password", "cretzuel"])


def test_the_policy_of_resumes_toml():
    assert config.policy_of({}) == {}
    p = config.policy_of({"signup": {"enabled": True, "sms_per_day": 20, "sms_countries": ["ro", " de"], "work_email": False}, "limits": {"usd_per_day": 2, "turns_per_day": 50}})
    assert p == {"signup": {"sms_per_day": 20, "sms_countries": ["RO", "DE"], "work_email": False}, "limits": {"usd_per_day": 2.0, "turns_per_day": 50}}
    for bad in ({"signup": {"sms_per_dya": 1}}, {"limits": {"usd_per_day": -1}}, {"signup": {"sms_per_day": "50"}},
                {"signup": {"sms_countries": "RO"}}, {"limits": {"turns_per_day": True}}, {"signup": {"work_email": 0}}):
        with pytest.raises(ValueError):
            config.policy_of(bad)


def test_the_launcher_hands_signup_and_policy_to_the_app_server(tmp_path: Path, monkeypatch):
    from agentic_search.web import launch

    root = tmp_path
    (root / "resumes.toml").write_text('[web]\nport = 8799\n[signup]\nenabled = true\nsms_per_day = 7\n[limits]\nusd_per_day = 0.5\n')
    cfg = config.load(root)
    assert cfg.web.signup is True and cfg.web.policy == {"signup": {"sms_per_day": 7}, "limits": {"usd_per_day": 0.5}}
    seen: dict = {}

    class Stop(Exception):
        pass

    def popen(args, **kw):
        seen["args"] = args
        raise Stop

    class FakeServer:
        should_exit = False

        def run(self):
            pass

    class FakeApp:
        class state:  # noqa: N801
            class hub:  # noqa: N801
                class index:  # noqa: N801
                    version = "v"
                    all_docs = []

    monkeypatch.setattr(launch, "check_node", lambda n: "node")
    monkeypatch.setattr(launch, "prepare_app", lambda d, n, log: Path("server.js"))
    monkeypatch.setattr(launch, "wait_ready", lambda url, timeout=0: None)
    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    import agentic_search.web.app as app_mod
    monkeypatch.setattr(app_mod, "make_server", lambda c: (FakeServer(), FakeApp()))
    with pytest.raises(Stop):
        launch.serve(cfg, log=lambda *a: None)
    args = seen["args"]
    assert "--signup" in args
    assert json.loads(args[args.index("--policy") + 1]) == {"signup": {"sms_per_day": 7}, "limits": {"usd_per_day": 0.5}}
    with pytest.raises(Stop):
        launch.serve(cfg, signup=False, log=lambda *a: None)
    assert "--signup" not in seen["args"], "`resumes web` without --signup on a machine whose toml says no … and --signup=False wins"
