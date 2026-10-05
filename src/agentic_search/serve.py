"""`resumes serve`: a warm embedder behind a Unix socket.

The CLI is a fresh process per call and cannot keep a model or an HTTPS connection warm; this server loads
the embedder once and answers `{"texts": [...]}` with `{"vecs": [...]}`, one JSON line each. The query path
falls back to a direct call when there is no socket (`query/text.py`).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import socketserver
import sys
import threading
from pathlib import Path

from .config import Config

CLIENT_TIMEOUT = 15.0


def socket_path(cfg: Config) -> Path:
    return cfg.query.serve_socket


def embed_via_socket(path: Path, texts: list[str]) -> list[list[float]] | None:
    """Vectors from a running `resumes serve`, or None when there is none (the caller falls back)."""
    if not path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(CLIENT_TIMEOUT)
            s.connect(str(path))
            s.sendall((json.dumps({"texts": texts}) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf.decode("utf-8"))
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["vecs"]
    except (OSError, ValueError):
        return None


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        try:
            req = json.loads(line.decode("utf-8"))
            vecs = self.server.embedder.embed(list(req["texts"]))  # type: ignore[attr-defined]
            reply = {"vecs": vecs}
        except Exception as e:  # noqa: BLE001 — the client gets the reason and falls back
            reply = {"error": f"{type(e).__name__}: {e}"}
        self.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.served += 1  # type: ignore[attr-defined]


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(cfg: Config, *, embedder_spec: str | None = None, path: Path | None = None, log=print) -> int:
    from .index.embed import make_embedder
    from .index.store import open_current, read_meta

    spec, dim = embedder_spec, cfg.index.dim
    if spec is None:
        try:
            con = open_current(cfg.index.out)
            meta = read_meta(con)
            con.close()
            spec, dim = meta.get("embedder", cfg.index.embedder), int(meta.get("dim", cfg.index.dim))
        except FileNotFoundError:
            spec = cfg.index.embedder
    embedder = make_embedder(spec, dim, None)
    embedder.embed(["warm-up"])
    path = path or socket_path(cfg)
    if len(str(path)) > 100:      # the Unix socket path limit is 104 bytes on macOS
        raise SystemExit(f"SOCKET_PATH_TOO_LONG {len(str(path))} chars: pass a shorter --socket (e.g. /tmp/resumes.sock)")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = _Server(str(path), _Handler)
    server.embedder, server.lock, server.served = embedder, threading.Lock(), 0  # type: ignore[attr-defined]
    log(f"resumes serve: {spec} (dim {dim}) on {path}  — Ctrl-C to stop")

    def _stop(*_) -> None:          # SIGTERM (e.g. from a supervisor) cleans up like Ctrl-C
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if path.exists():
            path.unlink()
        log(f"resumes serve: stopped after {server.served} request(s)")  # type: ignore[attr-defined]
    return 0
