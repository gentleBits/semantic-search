"""`resumes engine`: the engine behind an HTTP API, called by the app server (web-ts/) for every step.

There is no login: the engine listens on 127.0.0.1, and to block DNS rebinding and cross-site requests the
`Host` must be its own address, an `Origin` must be its own, and a body must be declared as JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..config import Config
from ..errors import ResumesError
from ..query import verbs, view
from . import actions, engine, state
from .hub import Conversation, Hub

LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "testserver"}
STATUS = {"UNKNOWN_SESSION": 404, "UNKNOWN_DOC_ID": 404, "BAD_SESSION_ID": 400, "INDEX_NOT_BUILT": 503, "RANKING_RUNNING": 409, "BUSY": 409,
          "NO_JOB": 409}
MAX_BODY = 400_000
PREVIEW_CHARS = 160


def _default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=_default)


class Json(JSONResponse):
    def render(self, content) -> bytes:
        return dumps(content).encode("utf-8")


def problem(e: ResumesError, conv: Conversation | None = None) -> Json:
    text = e.message or e.code
    if e.code == "UNKNOWN_SESSION":            # a tab left open on a conversation that is gone
        text = f"No conversation “{e.message}” on this server — start a new one (+) or pick one from the list."
    body: dict = {"error": {"role": "error", "code": e.code, "text": text}}
    if conv is not None:
        with conv.hub.lock:
            try:
                body["error"] = actions.friendly(conv.ctx, conv.web, e)
                body["state"] = conv.snap()
            except ResumesError:
                pass
    return Json(body, status_code=STATUS.get(e.code, 422))


async def body_of(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ResumesError("BAD_ARGUMENT", "the message is too long")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ResumesError("BAD_ARGUMENT", "the request is not JSON") from None
    if not isinstance(data, dict):
        raise ResumesError("BAD_ARGUMENT", "the request is not an object")
    return data


def collection(hub: Hub) -> dict:
    cfg, index = hub.cfg, hub.index
    w = cfg.web
    return {
        "name": w.name, "noun": w.noun, "nouns": w.nouns, "document": w.document, "starters": w.starters,
        "attach": w.attach, "demo": w.demo,
        "count": view.people(index, index.all_docs), "documents": len(index.all_docs), "index_version": index.version, "built_at": index.meta.get("built_at"),
        "currency": cfg.currency, "symbol": state.render.rate_symbol(cfg.currency),
        "limit": cfg.query.max_cards, "page_size": cfg.query.page_size,
        "sort_keys": [{"key": "judgment", "name": "Ranking", "dir": "desc"}, {"key": "rate", "name": "Rate", "dir": "asc"},
                      {"key": "years", "name": "Years", "dir": "desc"}, {"key": "seniority", "name": "Seniority", "dir": "desc"}],
        "seniorities": list(actions.SENIORITIES),
        "availabilities": [{"code": c, "label": state.avail_label(c)} for c in ("now", "2w", "1m", "3m")],
        "commands": actions.HELP,
        "changes": sorted(actions.CHANGES),
        "topics": [t.canonical for t in index.vocab.terms if t.kind == "topic"],
        "fields": hub.facts(),
        "sessions_dir": str(cfg.query.sessions.resolve()),         # the app server keeps the agent's memory next to the session files
        "defaults": {"model": w.model, "effort": w.effort, "judge_model": w.judge_model, "judge_effort": w.judge_effort},
        "judge": {"batch": w.judge_batch, "parallel": w.judge_parallel, "note_max": engine.rank.NOTE_MAX},
    }


def attachment_of(web, a: dict) -> dict | None:
    """Save a job description as jd-N.md → the facts the transcript shows for it."""
    text = str(a.get("text") or "")
    if not text.strip():
        return None
    fname = web.save_jd(text, a.get("name") if isinstance(a.get("name"), str) else None)
    body = text.strip()
    return {"file": fname, "name": web.jd_name(fname), "lines": len([ln for ln in body.splitlines() if ln.strip()]), "chars": len(body),
            "preview": " ".join(body.split())[:PREVIEW_CHARS]}


def create_app(cfg: Config) -> Starlette:
    hub = Hub(cfg)

    def conv_of(request: Request) -> Conversation:
        return hub.get(request.path_params["sid"])

    def opened(conv: Conversation) -> dict:
        with hub.lock:
            return {"session": {"id": conv.id, "title": conv.web.title}, "chat": conv.web.chat, "busy": conv.ranking, "state": conv.snap()}

    async def config(request: Request) -> Response:
        try:
            return Json(await run_in_threadpool(lambda: _locked(hub, lambda: collection(hub))))
        except ResumesError as e:
            return problem(e)

    async def sessions(request: Request) -> Response:
        if request.method == "POST":
            conv = await run_in_threadpool(hub.new)
            return Json(await run_in_threadpool(opened, conv), status_code=201)
        return Json({"sessions": await run_in_threadpool(hub.list)})

    async def session(request: Request) -> Response:
        try:
            conv = await run_in_threadpool(conv_of, request)
            if request.method == "DELETE":
                await run_in_threadpool(hub.delete, conv.id)
                return Json({"deleted": conv.id})
            return Json(await run_in_threadpool(opened, conv))
        except ResumesError as e:
            return problem(e)

    async def snapshot(request: Request) -> Response:
        try:
            conv = await run_in_threadpool(conv_of, request)
            overview = request.query_params.get("overview") not in ("0", "no", "false")
            return Json(await run_in_threadpool(lambda: _locked(hub, lambda: {"state": conv.snap(with_overview=overview), "busy": conv.ranking})))
        except ResumesError as e:
            return problem(e)

    async def action(request: Request) -> Response:
        conv = None
        try:
            conv = await run_in_threadpool(conv_of, request)
            data = await body_of(request)
            return Json(await run_in_threadpool(lambda: _locked(hub, lambda: actions.act(conv.ctx, conv.web, data, job=conv.job_view()))))
        except ResumesError as e:
            return problem(e, conv)

    async def person(request: Request) -> Response:
        conv = None
        try:
            conv = await run_in_threadpool(conv_of, request)
            ref = request.path_params["ref"]
            return Json(await run_in_threadpool(lambda: _locked(hub, lambda: state.person(conv.ctx, conv.web, ref))))
        except ResumesError as e:
            return problem(e, conv)

    async def text(request: Request) -> Response:
        try:
            conv = await run_in_threadpool(conv_of, request)
            ref = request.path_params["ref"]
            p = await run_in_threadpool(lambda: _locked(hub, lambda: state.person(conv.ctx, conv.web, ref)))
            return Response(f"# {p['title']}\n\n{p['markdown']}", media_type="text/plain; charset=utf-8")
        except ResumesError as e:
            return problem(e)

    async def ids(request: Request) -> Response:
        """`?places=1,2,13` → {"1": "r00…"}: the resume an answer's #N refers to."""
        try:
            conv = await run_in_threadpool(conv_of, request)
            places = [int(p) for p in request.query_params.get("places", "").split(",") if p.strip().isdigit()]

            def look() -> dict:
                _, rs = verbs._current(conv.ctx)
                wanted = [rs.order[k - 1] for k in places if 1 <= k <= rs.count]
                names = conv.ctx.index.ids(wanted)
                return {str(k): names.get(rs.order[k - 1]) for k in places if 1 <= k <= rs.count}

            return Json(await run_in_threadpool(lambda: _locked(hub, look)))
        except (ResumesError, ValueError) as e:
            return problem(e if isinstance(e, ResumesError) else ResumesError("BAD_ARGUMENT", str(e)))

    async def messages(request: Request) -> Response:
        conv = None
        try:
            conv = await run_in_threadpool(conv_of, request)
            data = await body_of(request)
            msg = data.get("message")
            if not isinstance(msg, dict) or not msg.get("role"):
                raise ResumesError("BAD_ARGUMENT", "message is an object with a role")

            def append() -> dict:
                web = conv.web
                if msg["role"] == "user":
                    a = msg.get("attachment")
                    att = attachment_of(web, a) if isinstance(a, dict) else None
                    said = str(msg.get("text") or "").strip()
                    web.name_from(said or (att["name"] if att else ""))
                    return web.append({"role": "user", "text": said, **({"attachment": att} if att else {})})
                return web.append({k: v for k, v in msg.items() if k not in ("id", "ts")})

            return Json({"message": await run_in_threadpool(lambda: _locked(hub, append))})
        except ResumesError as e:
            return problem(e, conv)

    async def slash(request: Request) -> Response:
        conv = None
        try:
            conv = await run_in_threadpool(conv_of, request)
            data = await body_of(request)
            said = str(data.get("text") or "").strip()
            if not said.startswith("/"):
                raise ResumesError("BAD_ARGUMENT", "a slash command starts with /")

            def run() -> dict:
                user = conv.web.append({"role": "user", "text": said, "mono": True})
                try:
                    out = actions.slash(conv.ctx, conv.web, said, job=conv.job_view())
                except ResumesError as e:
                    err = conv.web.append(actions.friendly(conv.ctx, conv.web, e))
                    return {"user": user, "error": err, "state": conv.snap()}
                if "handoff" in out:
                    return {"user": user, "handoff": out["handoff"]}
                return {"user": user, "messages": out["messages"], "state": out["state"], "ui": out["ui"]}

            return Json(await run_in_threadpool(lambda: _locked(hub, run)))
        except ResumesError as e:
            return problem(e, conv)

    async def tool(request: Request) -> Response:
        conv = None
        try:
            conv = await run_in_threadpool(conv_of, request)
            data = await body_of(request)
            name, args = str(data.get("name") or ""), data.get("args") if isinstance(data.get("args"), dict) else {}
            return Json(await run_in_threadpool(lambda: _locked(hub, lambda: engine.call(conv, name, args))))
        except ResumesError as e:
            return problem(e, conv)

    async def rank_prepare(request: Request) -> Response:
        conv = None
        try:
            conv = await run_in_threadpool(conv_of, request)
            data = await body_of(request)
            return Json(await run_in_threadpool(lambda: _locked(hub, lambda: engine.rank_prepare(conv, data))))
        except ResumesError as e:
            return problem(e, conv)

    async def rank_progress(request: Request) -> Response:
        conv = None
        try:
            conv = await run_in_threadpool(conv_of, request)
            data = await body_of(request)
            return Json(await run_in_threadpool(lambda: _locked(hub, lambda: engine.rank_progress(conv, list(data.get("scores") or [])))))
        except ResumesError as e:
            return problem(e, conv)

    async def rank_finish(request: Request) -> Response:
        conv = None
        try:
            conv = await run_in_threadpool(conv_of, request)
            data = await body_of(request)
            return Json(await run_in_threadpool(lambda: _locked(hub, lambda: engine.rank_finish(conv, data))))
        except ResumesError as e:
            return problem(e, conv)

    routes = [
        Route("/api/config", config),
        Route("/api/sessions", sessions, methods=["GET", "POST"]),
        Route("/api/sessions/{sid}", session, methods=["GET", "DELETE"]),
        Route("/api/sessions/{sid}/state", snapshot),
        Route("/api/sessions/{sid}/action", action, methods=["POST"]),
        Route("/api/sessions/{sid}/people/{ref}", person),
        Route("/api/sessions/{sid}/people/{ref}/text", text),
        Route("/api/sessions/{sid}/ids", ids),
        Route("/api/sessions/{sid}/messages", messages, methods=["POST"]),
        Route("/api/sessions/{sid}/slash", slash, methods=["POST"]),
        Route("/api/sessions/{sid}/tool", tool, methods=["POST"]),
        Route("/api/sessions/{sid}/rank/prepare", rank_prepare, methods=["POST"]),
        Route("/api/sessions/{sid}/rank/progress", rank_progress, methods=["POST"]),
        Route("/api/sessions/{sid}/rank/finish", rank_finish, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.state.hub = hub
    app.add_middleware(OwnPageOnly, hosts=LOCAL_HOSTS | {cfg.web.host.lower()})
    return app


class OwnPageOnly:
    """Refuses requests that do not come from this server's own callers (see the module docstring)."""

    def __init__(self, app, hosts: set[str]) -> None:
        self.app = app
        self.hosts = hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        origin = headers.get("origin")
        why = None
        if name.lower() not in self.hosts:
            why = f"this server answers as {', '.join(sorted(self.hosts - {'testserver'}))}, not as {name or '?'}"
        elif origin and origin.split("://", 1)[-1] != host:
            why = "requests from other sites are not answered"
        elif scope["path"].startswith("/api/") and scope["method"] in ("POST", "PUT", "PATCH") \
                and int(headers.get("content-length") or 0) > 0 and not headers.get("content-type", "").startswith("application/json"):
            why = "send JSON (Content-Type: application/json)"
        if why:
            await Json({"error": {"role": "error", "code": "FORBIDDEN", "text": why}}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _locked(hub: Hub, fn):
    with hub.lock:
        hub.refresh()
        return fn()


def make_server(cfg: Config, *, host: str | None = None, port: int | None = None):
    import uvicorn

    app = create_app(cfg)
    config = uvicorn.Config(app, host=host or "127.0.0.1", port=port or cfg.web.engine_port, log_level="warning")
    return uvicorn.Server(config), app


def serve_engine(cfg: Config, *, host: str | None = None, port: int | None = None, log=print) -> int:
    server, app = make_server(cfg, host=host, port=port)
    hub: Hub = app.state.hub
    index = hub.index
    log = (lambda *a: print(*a, flush=True)) if log is print else log
    log(f"resumes engine · http://{server.config.host}:{server.config.port} · index {index.version} ({len(index.all_docs):,} documents) · sessions {cfg.query.sessions}")
    server.run()
    return 0


__all__ = ["create_app", "serve_engine", "make_server", "Path"]
