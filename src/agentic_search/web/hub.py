"""What the engine service keeps in memory: one index, one lock, the open conversations.

The engine is not thread-safe (one DuckDB connection, session files written in steps), so every touch of it
happens under `Hub.lock`; the steps take milliseconds.
"""

from __future__ import annotations

import re
import threading

from ..config import Config
from ..errors import ResumesError
from ..index.store import current_path
from ..query.index import Index
from ..query.verbs import Context
from . import state
from .rank import Job
from .store import WebSession, list_web_sessions


class Conversation:
    def __init__(self, hub: "Hub", web: WebSession) -> None:
        self.hub = hub
        self.web = web
        self.ctx = Context(hub.cfg, session_id=web.id, tty=False)
        self.ctx._index = hub.index
        self.job: Job | None = None                    # the running ranking job; the app server posts its scores

    @property
    def id(self) -> str:
        return self.web.id

    @property
    def ranking(self) -> bool:
        return self.job is not None and self.job.running

    def job_view(self) -> dict | None:
        return self.job.public() if self.job is not None else None

    def snap(self, **kw) -> dict:
        return state.snapshot(self.ctx, self.web, job=self.job_view(), per_round=self.hub.per_round, **kw)


def field_facts(index: Index) -> dict:
    """Per person: how many state each field, and what the values look like."""
    from ..query.index import _in_list
    from ..query.search import availability_label

    docs = _in_list(list(index.representatives))
    con = index.con
    n, loc, rate, est, years, avail, remote = con.execute(
        "SELECT count(*), count(location), count(*) FILTER (WHERE rate IS NOT NULL AND rate_source <> 'synthetic'), count(*) FILTER (WHERE rate_source = 'synthetic'), "
        f"count(years), count(availability), count(*) FILTER (WHERE remote = true) FROM profile WHERE doc_no {docs}").fetchone()
    common = [v for v, in con.execute(f"SELECT location FROM profile WHERE doc_no {docs} AND location IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 5").fetchall()]
    other = [v for v, in con.execute(f"SELECT location FROM profile WHERE doc_no {docs} AND location IS NOT NULL AND location NOT LIKE '%,%' AND length(location) < 40 "
                                     "GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 2").fetchall()]
    labels: dict[str, int] = {}
    for a, c in con.execute(f"SELECT availability, count(*) FROM profile WHERE doc_no {docs} AND availability IS NOT NULL GROUP BY 1").fetchall():
        lab = availability_label(a)
        lab = lab if re.fullmatch(r"now|\d+[dwm]", lab or "") else "other"
        labels[lab] = labels.get(lab, 0) + int(c)
    return {
        "people": int(n),
        "location": {"stated": int(loc), "examples": common + [v for v in other if v not in common]},
        "rate": {"stated": int(rate), "estimated": int(est), "unknown": int(n - rate - est)},
        "years": {"known": int(years), "unknown": int(n - years)},
        "availability": {"stated": int(avail), "values": sorted(labels.items(), key=lambda x: -x[1])[:6]},
        "remote": {"stated": int(remote)},
    }


class Hub:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.lock = threading.RLock()
        self.conversations: dict[str, Conversation] = {}
        self.per_round: float | None = None            # seconds one round of judging took in the last job
        self._index: Index | None = None
        self._index_path = None
        self._facts: tuple[str, dict] | None = None       # (index version, field facts)

    # -- the index
    @property
    def index(self) -> Index:
        """Reopened when `index/current` points at a new build."""
        path = current_path(self.cfg.index.out)
        if self._index is None or path != self._index_path:
            self._index = Index(self.cfg)
            self._index_path = path
            for c in self.conversations.values():
                c.ctx._index = self._index
                c.ctx.forget_judgments()
        return self._index

    def refresh(self) -> None:
        self.index  # noqa: B018

    def facts(self) -> dict:
        index = self.index
        if self._facts is None or self._facts[0] != index.version:
            self._facts = (index.version, field_facts(index))
        return self._facts[1]

    # -- conversations
    def new(self, sid: str | None = None) -> Conversation:
        with self.lock:
            web = WebSession.create(self.cfg.query.sessions, sid)
            c = Conversation(self, web)
            self.conversations[web.id] = c
            return c

    def get(self, sid: str) -> Conversation:
        with self.lock:
            self.refresh()
            c = self.conversations.get(sid)
            if c is None:
                c = Conversation(self, WebSession.open(self.cfg.query.sessions, sid))
                self.conversations[sid] = c
            return c

    def list(self) -> list[dict]:
        rows = list_web_sessions(self.cfg.query.sessions)
        for r in rows:
            c = self.conversations.get(r["id"])
            r["busy"] = bool(c and c.ranking)
        return rows

    def delete(self, sid: str) -> None:
        import shutil

        with self.lock:
            c = self.get(sid)
            if c.ranking:
                raise ResumesError("RANKING_RUNNING", "")
            self.conversations.pop(sid, None)
            shutil.rmtree(c.web.dir, ignore_errors=True)
