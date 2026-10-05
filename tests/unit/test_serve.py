"""`resumes serve` protocol: a warm embedder behind a Unix socket, with fallback when it is absent."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from agentic_search.query.text import QueryEmbedder
from agentic_search.serve import embed_via_socket

ROOT = Path(__file__).resolve().parents[2]


def test_socket_roundtrip_and_fallback(tmp_path):
    import os

    sock = Path(f"/tmp/resumes-test-{os.getpid()}.sock")     # Unix socket paths are limited to 104 bytes; pytest's tmp_path is longer
    assert embed_via_socket(sock, ["x"]) is None, "no server → None → the caller falls back"
    proc = subprocess.Popen([sys.executable, "-m", "agentic_search.cli", "serve", "--embedder", "fake:test", "--socket", str(sock)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.05)
        assert sock.exists(), proc.stdout.read() if proc.poll() is not None else "socket never appeared"
        vecs = embed_via_socket(sock, ["hello", "world"])
        assert vecs is not None and len(vecs) == 2 and len(vecs[0]) == 1024 and abs(sum(x * x for x in vecs[0]) - 1) < 1e-6
        assert embed_via_socket(sock, ["hello"]) == [vecs[0]], "deterministic"
        qe = QueryEmbedder("openai:text-embedding-3-large", 1024, tmp_path / "cache", sock)
        assert qe.embed("hello") == vecs[0], "the query path takes the socket before any API call"
        assert qe.embed_many(["hello", "again"])[0] == vecs[0]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert not sock.exists(), "the socket file is removed on shutdown"
