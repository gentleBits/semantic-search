"""The engine half of the ranking job: who is scored, and how scores land. The model calls run in web-ts/.

Nobody is scored twice for one criterion: a later pass adds only the people without a score. Above `max_cards`
people the job refuses and keeps the criterion as pending. Stopping keeps what was scored.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..errors import ResumesError
from ..query import facets as facets_mod
from ..query import render, verbs
from ..query.verbs import Context
from ..session import judgments as jmod
from . import actions
from .state import estimate
from .store import WebSession

ANCHORS = 3
NOTE_MAX = jmod.NOTE_MAX


@dataclass
class Job:
    set_id: str
    criterion: str
    into: str | None                       # the judgment these scores are added to; None: a new one
    docs: list[int]                        # the people to score
    total: int                             # people in the set
    already: int                           # scored before under this criterion
    ids: dict[int, str]
    cards: dict[int, str]
    context: str
    anchors: list[str]
    scores: dict[int, tuple[int, str]] = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    ended: float | None = None
    running: bool = True
    stopped: bool = False
    error: str | None = None
    usage: dict = field(default_factory=lambda: {"in": 0, "out": 0, "cached": 0, "calls": 0, "cost": 0.0})
    estimate: float = 0.0

    @property
    def seconds(self) -> int:
        return int(round((self.ended or time.time()) - self.started))

    def public(self) -> dict:
        return {"running": self.running, "set": self.set_id, "criterion": self.criterion, "done": len(self.scores), "total": len(self.docs),
                "people": self.total, "already": self.already, "seconds": self.seconds, "estimate": int(round(self.estimate)),
                "stopped": self.stopped, "scores": dict(self.scores), "waiting": set(self.docs) - set(self.scores)}

    def view(self) -> dict:
        return {"set": self.set_id, "criterion": self.criterion, "into": self.into, "total": self.total, "already": self.already,
                "context": self.context, "anchors": list(self.anchors), "estimate": int(round(self.estimate)),
                "people": [{"doc": d, "id": self.ids[d], "card": self.cards[d]} for d in self.docs]}

    def land(self, scores: list) -> int:
        """Scores posted by the app server ({id, score, note}) → how many were for this job's people."""
        by_id = {i: d for d, i in self.ids.items()}
        n = 0
        for s in scores or []:
            if not isinstance(s, dict) or s.get("id") not in by_id:
                continue
            try:
                score = int(round(max(0.0, min(100.0, float(s.get("score"))))))
            except (TypeError, ValueError):
                continue
            note = " ".join(str(s.get("note") or "").split())
            if len(note) > NOTE_MAX:
                note = note[: NOTE_MAX - 1].rstrip() + "…"
            self.scores[by_id[s["id"]]] = (score, note)
            n += 1
        return n


def describe_set(ctx: Context, rs) -> str:
    f = facets_mod.compute(ctx.index, rs.bitmap())
    r, y = f.get("rate") or {}, f.get("years") or {}
    sym = render.rate_symbol(r.get("currency"))
    bits = [f"{rs.count} people"]
    if r.get("p50") is not None:
        bits.append(f"rate per hour: lower quartile {sym}{r['p25']}, median {sym}{r['p50']}, upper quartile {sym}{r['p75']}")
    if y.get("p50") is not None:
        bits.append(f"years of experience: median {y['p50']} (quartiles {y['p25']}–{y['p75']})")
    if r.get("synthetic"):
        bits.append(f"the rate is estimated for {r['synthetic']} of them")
    return "; ".join(bits)


def prepare(ctx: Context, web: WebSession, criterion: str | None, *, fresh: bool = False, per_round: float | None = None) -> Job:
    """Who is to be scored, under which judgment. On TOO_MANY_TO_RANK the criterion is kept as pending."""
    session, rs = verbs._current(ctx)
    limit = ctx.cfg.query.max_cards
    shown_jid, _ = verbs._judgment_for(ctx, session, rs, ignore_off=True)
    shown = jmod.criterion_of(session, shown_jid, ctx.index.con) if shown_jid else None
    given = " ".join((criterion or "").split()).strip(" .\"“”'")
    criterion = given or (web.pending or {}).get("criterion") or shown
    if not criterion:
        raise ResumesError("NO_CRITERION", "rank them for what? say it in a few words")
    if rs.count == 0:
        raise ResumesError("EMPTY_SET", rs.id)
    if rs.count > limit:
        web.set_pending(criterion)
        raise verbs._too_many(ctx, session, rs)
    into = None
    if not fresh:
        same = [j["id"] for j in jmod.list_judgments(session, ctx.index.con) if jmod.same_criterion(j["criterion"], criterion)]
        into = same[-1] if same else (shown_jid if not given else None)
    scores = ctx.scores(session, into) if into else {}
    if into:
        criterion = jmod.criterion_of(session, into, ctx.index.con) or criterion
    docs = [d for d in rs.order if scores.get(d, (None, ""))[0] is None]
    texts, versions = ctx.index.cards(docs), verbs._versions_of(ctx, docs)
    docs = [d for d in docs if d in texts]
    cards = {d: render.card_text(texts[d], None, versions.get(d, 0)) for d in docs}
    anchors: list[str] = []
    if into:
        ranked = sorted(((s, d) for d, (s, _) in scores.items() if s is not None), key=lambda x: (-x[0], x[1]))
        picked = [d for _, d in ranked[:ANCHORS]] + [d for _, d in ranked[-ANCHORS:] if d not in {x for _, x in ranked[:ANCHORS]}]
        heads = ctx.index.cards(picked)
        anchors = [f"★{scores[d][0]} {heads[d].splitlines()[0].split(' · ', 1)[1]} — {scores[d][1]}" for d in picked if d in heads]
    context = (web.meta.get("judge_context") or {}).get(into) if into else None
    job = Job(set_id=rs.id, criterion=criterion, into=into, docs=docs, total=rs.count, already=rs.count - len(docs), ids=ctx.index.ids(docs),
              cards=cards, context=context or describe_set(ctx, rs), anchors=anchors)
    job.estimate = estimate(ctx.cfg, len(docs), per_round)
    return job


def finish(ctx: Context, web: WebSession, job: Job, judge_name: str) -> dict:
    """Record what was scored and sort the list by the ranking."""
    session = ctx.session()
    rs = session.get(job.set_id)
    jid = job.into
    scored = len(job.scores)
    if scored:
        payload = {"criterion": job.criterion, "judge": f"{judge_name} via resumes web",
                   "scores": [{"id": job.ids[d], "score": s, "note": n} for d, (s, n) in job.scores.items()]}
        out = verbs.score(ctx, rs.id, text=json.dumps(payload, ensure_ascii=False), allow_partial=True, into=job.into)
        jid = out.data["judgment"]
        web.meta.setdefault("judge_context", {})[jid] = job.context
    if jid and (session.current() or rs).id == rs.id:
        actions.sort(ctx, web, "judgment,rate", jid)
    if scored or job.stopped or not job.error:        # keep the criterion pending only when the model failed outright
        web.set_pending(None)
    else:
        web.save()
    after = session.current()
    _, scores = verbs._judgment_for(ctx, session, after)
    judged = verbs._n_judged(after, scores)
    per_round = None
    if scored and job.ended:
        rounds = max(1, -(-len(job.docs) // (ctx.cfg.web.judge_batch * ctx.cfg.web.judge_parallel)))
        per_round = (job.ended - job.started) / rounds
    return {"judgment": jid, "criterion": job.criterion, "scored": scored, "asked": len(job.docs), "already": job.already, "judged": judged,
            "total": after.count, "seconds": job.seconds, "stopped": job.stopped, "error": job.error, "usage": dict(job.usage),
            "merged": job.into is not None, "set": after.id, "per_round": per_round}
