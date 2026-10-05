"""Load resumes.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Source:
    id: str
    loader: str
    path: Path
    optional: bool = False


@dataclass
class IndexConfig:
    out: Path
    cache: Path
    vocab: Path
    embedder: str = "openai:text-embedding-3-large"
    dim: int = 1024
    extractor: str = "openai:gpt-5-mini"
    extract_concurrency: int = 16
    tau: float = 0.45
    chunk_max_tokens: int = 350
    chunk_overlap: int = 40
    card_max_tokens: int = 110


@dataclass
class QueryConfig:
    sessions: Path
    max_cards: int = 50                  # `cards` / `judge` refuse above this many people and ask to narrow
    page_size: int = 10
    gc_days: int = 14
    tau_text: float = 0.35               # --text membership threshold; below `tau` because short queries sit further from chunks
    like_min_cover: float = 0.4          # --like membership: a document must cover this share of the JD's resolved terms
    serve_socket: Path = Path(".resumes/serve.sock")   # `resumes serve` listens here; the CLI uses it when present
    allow_contact: bool = False          # `show --full --contact` reads the unredacted source
    link_template: str | None = None     # e.g. "https://ats.example/candidates/{id}"; default: file:// URL of corpus/md/<id>.md


STARTERS = ["Who worked on data pipelines?", "Senior backend people available now", "Who knows Elixir and Kubernetes?"]


@dataclass
class WebConfig:
    """`resumes web`: where it listens, which model talks and judges, and the words of the collection."""
    host: str = "127.0.0.1"              # login is required once `resumes users add` has made an account
    port: int = 8765                     # the app server (web-ts/)
    engine_port: int = 8770              # the Python engine's API, on 127.0.0.1 only
    app_dir: Path = Path("web-ts")       # relative to the project root
    state_dir: Path = Path(".resumes")   # the app server's files: settings.json (keys), users.json (accounts), owners.json, secret, cache/
    public_host: str | None = None       # behind a proxy: the public name(s) the app server answers as, e.g. "search.example.com"
    model: str = "openai:gpt-5-mini"     # the assistant (chat + tools); `fake:` replays a script (tests)
    effort: str | None = "medium"        # thinking level of the assistant, when the model takes one
    judge_model: str | None = None       # the ranking job; default: the assistant's model
    judge_effort: str | None = "low"
    judge_batch: int = 5                 # cards per call: small batches make the progress visible
    judge_parallel: int = 5
    name: str = "Resumes"
    noun: str = "person"
    nouns: str = "people"
    document: str = "resume"
    starters: list[str] = field(default_factory=lambda: list(STARTERS))
    attach: str = "a job description"    # what an added file usually is; shown only in the tooltip of "+ Add file" ("" = none)
    signup: bool = False                 # sign-up by email code + SMS code ([signup].enabled, or `resumes web --signup`)
    policy: dict = field(default_factory=dict)   # {"signup": {…}, "limits": {…}} from resumes.toml; the app server fills in the rest


# What [signup] and [limits] may say; the app server holds the defaults (web-ts/src/policy.ts).
SIGNUP_KEYS = {"sms_countries": list, "sms_per_day": int, "emails_per_day": int, "code_ttl": int, "code_tries": int, "codes_per_step": int,
               "resend_after": int, "flow_ttl": int, "per_email_day": int, "per_phone_day": int, "per_ip_flows_hour": int,
               "per_ip_emails_day": int, "per_ip_sms_day": int, "min_password": int, "work_email": bool}
LIMITS_KEYS = {"turns_per_day": int, "usd_per_day": float, "at_once": int, "sessions_per_day": int, "max_usd_per_m_out": float, "login_ip_fails": int, "login_ip_window": int, "login_ip_wait": int}


def policy_of(raw: dict) -> dict:
    """[signup] and [limits] → the JSON the app server takes (`--policy`); an unknown key or a wrong type is refused."""
    out: dict = {}
    for table, keys in (("signup", SIGNUP_KEYS), ("limits", LIMITS_KEYS)):
        got = raw.get(table, {}) or {}
        part = {}
        for k, v in got.items():
            if k == "enabled" and table == "signup":
                continue
            if k not in keys:
                raise ValueError(f"[{table}] {k}: unknown (known: {', '.join(sorted(keys))})")
            want = keys[k]
            if want is list:
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise ValueError(f"[{table}] {k}: a list of ISO country codes, e.g. [\"RO\", \"DE\"]")
                part[k] = [x.strip().upper() for x in v]
            elif want is bool:
                if not isinstance(v, bool):
                    raise ValueError(f"[{table}] {k}: true or false")
                part[k] = v
            elif want is float:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    raise ValueError(f"[{table}] {k}: a number ≥ 0")
                part[k] = float(v)
            else:
                if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    raise ValueError(f"[{table}] {k}: a whole number ≥ 0")
                part[k] = int(v)
        if part:
            out[table] = part
    return out


@dataclass
class Config:
    root: Path
    corpus_out: Path
    currency: str
    sources: list[Source] = field(default_factory=list)
    fixture_model: str = "gpt-5-mini"
    fixture_seed: int = 42
    fixture_count: int = 400
    index: IndexConfig | None = None
    query: QueryConfig | None = None
    web: WebConfig = field(default_factory=WebConfig)


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default: cwd) to the directory holding resumes.toml."""
    p = (start or Path.cwd()).resolve()
    for candidate in (p, *p.parents):
        if (candidate / "resumes.toml").is_file():
            return candidate
    raise FileNotFoundError("resumes.toml not found in this directory or any parent")


def _merge(base: dict, over: dict) -> dict:
    """`over` wins; tables merge key by key, anything else (lists such as [[sources]]) is replaced whole."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(root: Path | None = None) -> Config:
    root = root or find_root()
    raw = tomllib.loads((root / "resumes.toml").read_text(encoding="utf-8"))
    local = root / "resumes.local.toml"  # one installation's own settings on top of the shared ones
    if local.is_file():
        raw = _merge(raw, tomllib.loads(local.read_text(encoding="utf-8")))
    corpus = raw.get("corpus", {})
    fixture = raw.get("fixture", {})
    sources = [
        Source(
            id=s["id"],
            loader=s["loader"],
            path=root / s["path"],
            optional=bool(s.get("optional", False)),
        )
        for s in raw.get("sources", [])
    ]
    idx = raw.get("index", {})
    index = IndexConfig(
        out=Path(os.environ["RESUMES_INDEX_DIR"]).expanduser().resolve() if os.environ.get("RESUMES_INDEX_DIR") else root / idx.get("out", "index"),
        cache=root / idx.get("cache", "index/cache"),
        vocab=root / idx.get("vocab", "schema/vocab.csv"),
        embedder=idx.get("embedder", "openai:text-embedding-3-large"),
        dim=int(idx.get("dim", 1024)),
        extractor=idx.get("extractor", "openai:gpt-5-mini"),
        extract_concurrency=int(idx.get("extract_concurrency", 16)),
        tau=float(idx.get("tau", 0.45)),
        chunk_max_tokens=int(idx.get("chunk_max_tokens", 350)),
        chunk_overlap=int(idx.get("chunk_overlap", 40)),
        card_max_tokens=int(idx.get("card_max_tokens", 110)),
    )
    q = raw.get("query", {})
    query = QueryConfig(
        sessions=root / q.get("sessions", ".resumes/sessions"),
        max_cards=int(q.get("max_cards", 50)),
        page_size=int(q.get("page_size", 10)),
        gc_days=int(q.get("gc_days", 14)),
        tau_text=float(q.get("tau_text", 0.35)),
        like_min_cover=float(q.get("like_min_cover", 0.4)),
        serve_socket=root / q.get("serve_socket", ".resumes/serve.sock"),
        allow_contact=bool(q.get("allow_contact", False)),
        link_template=q.get("link_template") or None,
    )
    w = raw.get("web", {})
    d = WebConfig()
    web = WebConfig(
        host=str(w.get("host", d.host)),
        port=int(w.get("port", d.port)),
        engine_port=int(w.get("engine_port", d.engine_port)),
        app_dir=root / str(w.get("app_dir", d.app_dir)),
        state_dir=root / str(w.get("state_dir", d.state_dir)),
        public_host=str(w.get("public_host") or "").strip() or None,
        model=str(w.get("model", d.model)),
        effort=w.get("effort", d.effort) or None,
        judge_model=w.get("judge_model") or None,
        judge_effort=w.get("judge_effort", d.judge_effort) or None,
        judge_batch=max(1, int(w.get("judge_batch", d.judge_batch))),
        judge_parallel=max(1, int(w.get("judge_parallel", d.judge_parallel))),
        name=str(w.get("name", d.name)),
        noun=str(w.get("noun", d.noun)),
        nouns=str(w.get("nouns", d.nouns)),
        document=str(w.get("document", d.document)),
        starters=[str(x) for x in w.get("starters", d.starters)],
        attach=str(w.get("attach", d.attach)).strip(),
        signup=bool((raw.get("signup") or {}).get("enabled", False)),
        policy=policy_of(raw),
    )
    return Config(
        root=root,
        corpus_out=root / corpus.get("out", "corpus"),
        currency=corpus.get("currency", "EUR"),
        sources=sources,
        fixture_model=fixture.get("model", "gpt-5-mini"),
        fixture_seed=int(fixture.get("seed", 42)),
        fixture_count=int(fixture.get("count", 400)),
        index=index,
        query=query,
        web=web,
    )
