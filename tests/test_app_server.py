"""The app server's own Node tests (faux model, own engine) and its build (skipped without Node or its packages)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentic_search import config as config_mod
from agentic_search.index.store import current_path
from agentic_search.web.launch import find_node
from tests.testroot import TEST_ROOT

ROOT = Path(__file__).resolve().parents[1]
CFG = config_mod.load(TEST_ROOT)
APP = CFG.web.app_dir
NODE = find_node()
INSTALLED = (APP / "node_modules" / "@earendil-works" / "pi-agent-core").is_dir() and (APP / "node_modules" / "tsx").is_dir()
pytestmark = pytest.mark.skipif(not (NODE and INSTALLED), reason="node or the app server's packages are missing")


def test_the_app_servers_own_tests_pass():
    r = subprocess.run([NODE, "--import", "tsx", "--test", "--test-reporter=tap", "test/unit.test.ts", "test/turn.test.ts", "test/settings.test.ts", "test/login.test.ts"], cwd=APP, capture_output=True,
                       text=True, timeout=600)
    failed = r.stdout[r.stdout.find("not ok"):][:4000] if "not ok" in r.stdout else r.stdout[-3000:]
    assert r.returncode == 0, failed + r.stderr[-2000:]
    assert "# fail 0" in r.stdout
    if current_path(CFG.index.out) is not None:
        assert "# skipped 0" in r.stdout, "the index is built: nothing should be skipped"


def test_the_app_server_compiles():
    """`resumes web` runs dist/server.js, so the TypeScript must compile."""
    r = subprocess.run([NODE, str(APP / "node_modules" / "typescript" / "bin" / "tsc"), "-p", "tsconfig.json"], cwd=APP, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert (APP / "dist" / "server.js").is_file()
