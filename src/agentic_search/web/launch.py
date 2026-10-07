"""`resumes web`: the Python engine in this process, the TypeScript app server (web-ts/) as a child.

The app server is installed and compiled on demand. Stopping either side stops the other: the child exits
when its stdin closes, and the engine stops when the child is gone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..config import Config
from ..errors import ResumesError

NODE_MIN = (22, 19)  # pi 1.0's floor
NODE_CANDIDATES = ["/opt/homebrew/opt/node@22/bin/node", "/opt/homebrew/bin/node", "/usr/local/bin/node"]
READY_TIMEOUT = 60.0


def find_node() -> str | None:
    for c in [os.environ.get("RESUMES_NODE"), shutil.which("node"), *NODE_CANDIDATES]:
        if c and Path(c).is_file():
            return c
    return None


def check_node(node: str | None) -> str:
    if not node:
        raise ResumesError("WEB_UNAVAILABLE", "node not found: install Node 22.19 or newer (brew install node) for the web UI")
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise ResumesError("WEB_UNAVAILABLE", f"node does not run: {e}") from None
    parts = [int(x) if x.isdigit() else 0 for x in v.lstrip("v").split(".")[:2]] if v else []
    if tuple(parts + [0, 0])[:2] < NODE_MIN:
        raise ResumesError("WEB_UNAVAILABLE", f"node {v} is too old: the web UI needs Node {NODE_MIN[0]}.{NODE_MIN[1]}+")
    return node


def npm_of(node: str) -> str:
    npm = shutil.which("npm") or str(Path(node).with_name("npm"))
    if not Path(npm).is_file():
        raise ResumesError("WEB_UNAVAILABLE", f"npm not found next to {node}: run `npm install` in the app server's folder")
    return npm


def prepare_app(app_dir: Path, node: str, log) -> Path:
    """Install and compile the app server when needed → the script to run."""
    if not (app_dir / "package.json").is_file():
        raise ResumesError("WEB_UNAVAILABLE", f"the app server is missing: {app_dir}")
    if not (app_dir / "node_modules" / "@earendil-works" / "pi-agent-core").is_dir():
        log(f"resumes web · installing the app server's packages once (npm install in {app_dir}) …")
        r = subprocess.run([npm_of(node), "install", "--no-audit", "--no-fund"], cwd=app_dir, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise ResumesError("WEB_UNAVAILABLE", f"npm install failed in {app_dir}: {(r.stderr or r.stdout).strip()[-400:]}")
    script = app_dir / "dist" / "server.js"
    sources = list((app_dir / "src").rglob("*.ts"))
    newest = max((p.stat().st_mtime for p in sources), default=0.0)
    if not script.is_file() or script.stat().st_mtime < newest:
        log("resumes web · compiling the app server (tsc) …")
        r = subprocess.run([npm_of(node), "run", "--silent", "build"], cwd=app_dir, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not script.is_file():
            raise ResumesError("WEB_UNAVAILABLE", f"the app server did not compile: {(r.stderr or r.stdout).strip()[-600:]}")
    return script


def wait_ready(url: str, timeout: float = READY_TIMEOUT) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(0.1)
    raise ResumesError("WEB_UNAVAILABLE", f"{url} did not answer within {int(timeout)} s")


def serve(cfg: Config, *, host: str | None = None, port: int | None = None, open_browser: bool = False, public_host: str | None = None,
          signup: bool | None = None, log=print) -> int:
    from .app import make_server

    log = (lambda *a: print(*a, flush=True)) if log is print else log
    node = check_node(find_node())
    app_dir = cfg.web.app_dir
    script = prepare_app(app_dir, node, log)
    host, port = host or cfg.web.host, port or cfg.web.port
    engine_url = f"http://127.0.0.1:{cfg.web.engine_port}"

    server, app = make_server(cfg)
    hub = app.state.hub
    index = hub.index
    log(f"resumes web · engine {engine_url} · index {index.version} ({len(index.all_docs):,} documents)")
    engine_thread = threading.Thread(target=server.run, name="engine", daemon=True)
    engine_thread.start()
    wait_ready(f"{engine_url}/api/config")

    args = [node, str(script), "--host", host, "--port", str(port), "--engine", engine_url, "--state-dir", str(cfg.web.state_dir), "--exit-with-stdin"]
    public = (public_host or cfg.web.public_host or "").strip()
    if public:
        args += ["--public-host", public]          # behind a proxy: the name the browser sees, for the app server's Host check
    if signup if signup is not None else cfg.web.signup:
        args.append("--signup")                     # the sign-up keys come from the environment
    if cfg.web.policy:
        args += ["--policy", json.dumps(cfg.web.policy, separators=(",", ":"))]
    if open_browser:
        args.append("--open")
    child = subprocess.Popen(args, cwd=app_dir, stdin=subprocess.PIPE)

    def watch() -> None:
        child.wait()
        server.should_exit = True

    threading.Thread(target=watch, name="app-server", daemon=True).start()
    try:
        while not server.should_exit and child.poll() is None:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        if child.poll() is None:
            try:
                child.stdin.close()
                child.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                child.kill()
        engine_thread.join(timeout=5)
    return child.returncode if child.returncode not in (None, -15, -2) else 0


__all__ = ["serve", "find_node", "sys"]
