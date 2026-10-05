/**
 * The app server of `resumes web`: the page, the API (much of it proxied to the engine), and the assistant on pi.
 *
 *     node dist/server.js [--host 127.0.0.1] [--port 8765] [--engine http://127.0.0.1:8770] [--state-dir ../.resumes]
 *                         [--public-host NAME[,NAME]] [--signup] [--policy JSON] [--open] [--exit-with-stdin] [--faux]
 */
import { exec } from "node:child_process";
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { serve } from "@hono/node-server";
import { type Context, Hono } from "hono";
import { streamSSE } from "hono/streaming";
import { deleteCookie, getCookie, setCookie } from "hono/cookie";
import { isHttps } from "./auth.js";
import { EngineError, ResumesError } from "./errors.js";
import { type FeatureApi, loadFeatures } from "./features.js";
import { Hub, type HubOptions } from "./hub.js";
import { type Site, SignupError, ipKey } from "./signup.js";


const STATUS: Record<string, number> = { UNKNOWN_SESSION: 404, UNKNOWN_DOC_ID: 404, BAD_SESSION_ID: 400, INDEX_NOT_BUILT: 503, RANKING_RUNNING: 409, BUSY: 409, PROVIDER_UNREACHABLE: 502 };
const MAX_BODY = 400_000;
const TYPES: Record<string, string> = {
	".html": "text/html; charset=utf-8",
	".css": "text/css; charset=utf-8",
	".js": "text/javascript; charset=utf-8",
	".mjs": "text/javascript; charset=utf-8",
	".json": "application/json; charset=utf-8",
	".svg": "image/svg+xml",
	".png": "image/png",
	".ico": "image/x-icon",
	".woff2": "font/woff2",
	".woff": "font/woff",
	".txt": "text/plain; charset=utf-8",
	".md": "text/markdown; charset=utf-8",
};
export const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
const HERE = fileURLToPath(new URL(".", import.meta.url));
export const PUBLIC = resolve(HERE, "..", "public");

type ErrorBody = { error: { role: "error"; code: string; text: string; [k: string]: unknown }; state?: unknown };

const problem = (e: unknown): { status: number; body: ErrorBody } => {
	if (e instanceof SignupError) return { status: e.status, body: { error: { role: "error", code: e.code, text: e.message, ...e.extra } } };
	if (e instanceof EngineError) return { status: e.status || 502, body: (e.body as ErrorBody) || { error: { role: "error", code: e.code, text: e.text } } };
	if (e instanceof ResumesError) return { status: STATUS[e.code] ?? 422, body: { error: { role: "error", code: e.code, text: e.message || e.code } } };
	const err = e as Error;
	return { status: 500, body: { error: { role: "error", code: "INTERNAL", text: `Something went wrong here: ${err?.name || "Error"}: ${err?.message || String(e)}` } } };
};

async function bodyOf(c: Context): Promise<Record<string, unknown>> {
	const raw = await c.req.text();
	if (raw.length > MAX_BODY) throw new ResumesError("BAD_ARGUMENT", "the message is too long");
	if (!raw) return {};
	let data: unknown;
	try {
		data = JSON.parse(raw);
	} catch {
		throw new ResumesError("BAD_ARGUMENT", "the request is not JSON");
	}
	if (!data || typeof data !== "object" || Array.isArray(data)) throw new ResumesError("BAD_ARGUMENT", "the request is not an object");
	return data as Record<string, unknown>;
}

/** Only requests from this server's own page: a `Host` it serves (a name that merely resolves to 127.0.0.1 is DNS
 * rebinding), its own `Origin`, and bodies declared as JSON, which another site's page cannot send without asking first. */
export function ownPageOnly(hosts: Set<string>) {
	return async (c: Context, next: () => Promise<void>) => {
		const host = c.req.header("host") || new URL(c.req.url).host; // an in-process request (the tests) carries no Host header
		const name = host.endsWith("]") ? host : host.replace(/:\d+$/, "");
		const origin = c.req.header("origin");
		let why: string | null = null;
		if (!hosts.has(name.toLowerCase())) why = `this server answers as ${[...hosts].sort().join(", ")}, not as ${name || "?"}`;
		else if (origin && origin.split("://", 2)[1] !== host) why = "requests from other sites are not answered";
		else if (
			c.req.path.startsWith("/api/") &&
			["POST", "PUT", "PATCH"].includes(c.req.method) &&
			Number(c.req.header("content-length") || 0) > 0 &&
			!(c.req.header("content-type") || "").startsWith("application/json")
		) {
			why = "send JSON (Content-Type: application/json)";
		}
		if (why) return c.json({ error: { role: "error", code: "FORBIDDEN", text: why } }, 403);
		await next();
	};
}

/** The socket's address, or X-Forwarded-For's last entry when the peer is loopback (the proxy). An in-process request
 * (the tests) has no socket and counts as loopback. */
export function clientIp(peer: string | null | undefined, forwarded: string | null | undefined): string {
	const p = String(peer || "").replace(/^::ffff:(?=\d+\.)/, "");
	const loopback = !p || p === "127.0.0.1" || p === "::1";
	if (loopback && forwarded) {
		const last = forwarded.split(",").map((s) => s.trim()).filter(Boolean).at(-1);
		if (last) return last.replace(/^::ffff:(?=\d+\.)/, "");
	}
	return p || "127.0.0.1";
}

const ipOf = (c: Context): string => ipKey(clientIp((c.env as any)?.incoming?.socket?.remoteAddress, c.req.header("x-forwarded-for")));
const siteOf = (c: Context): Site => ({ host: c.req.header("host") || new URL(c.req.url).host, https: isHttps(c) });
export const SIGNUP_COOKIE = "resumes_signup";

const NO_SUCH = (sid: string) => ({ error: { role: "error", code: "UNKNOWN_SESSION", text: `No conversation “${sid}” on this server — start a new one (+) or pick one from the list.` } });

export type App = Hono<{ Variables: { user: string | null } }>;

export function createApp(hub: Hub, hosts: Set<string> = LOCAL_HOSTS, publicDir: string = PUBLIC): App {
	const app: App = new Hono();
	app.use("*", ownPageOnly(hosts));

	// while login is on, every /api route but login, logout, me and the sign-up needs the cookie
	app.use("/api/*", async (c, next) => {
		c.set("user", null);
		if (hub.auth.on() && !["/api/login", "/api/logout", "/api/me"].includes(c.req.path) && !(c.req.path === "/api/signup" || c.req.path.startsWith("/api/signup/"))) {
			const user = hub.auth.who(c);
			if (!user) return c.json({ error: { role: "error", code: "LOGIN_REQUIRED", text: "Log in to search." } }, 401);
			c.set("user", user);
		}
		await next();
	});
	// … and another person's conversation answers 404, exactly as an id that does not exist
	const mine = async (c: Context, next: () => Promise<void>) => {
		const id = String(c.req.param("sid") ?? "");
		if (hub.auth.on() && hub.owners.of(id) !== c.get("user")) return c.json(NO_SUCH(id), 404);
		await next();
	};
	app.use("/api/sessions/:sid", mine);
	app.use("/api/sessions/:sid/*", mine);

	const answer = async (c: Context, fn: () => Promise<unknown>, status = 200) => {
		try {
			return c.json((await fn()) as any, status as 200);
		} catch (e) {
			const p = problem(e);
			return c.json(p.body, p.status as 400);
		}
	};
	const sid = (c: Context) => String(c.req.param("sid") ?? "");
	const param = (c: Context, name: string) => String(c.req.param(name) ?? "");
	const withBusy = (c: Context, data: Record<string, any>) => ({ ...data, busy: !!data.busy || hub.busy(sid(c)) });

	app.get("/api/config", (c) =>
		answer(c, async () => {
			const cfg = await hub.engine.config();
			return { ...cfg, ...(await hub.assistantFields(cfg, c.get("user"))) };
		}),
	);
	app.get("/api/sessions", (c) =>
		answer(c, async () => {
			const { sessions } = await hub.engine.sessions();
			const user = c.get("user");
			const own = hub.auth.on() ? hub.owners.mine(user || "") : null;
			return { sessions: sessions.filter((s) => !own || own.has(s.id)).map((s) => ({ ...s, busy: !!s.busy || hub.busy(s.id) })) };
		}),
	);
	app.post("/api/sessions", (c) =>
		answer(
			c,
			async () => {
				const user = c.get("user");
				if (user && hub.limited(user)) hub.usage.mayStartSession(user, hub.owners.mine(user).size === 0); // SESSION_LIMIT
				const data = await hub.engine.newSession();
				if (user) {
					hub.owners.set(String(data.session?.id ?? ""), user);
					hub.usage.sessionMade(user, String(data.session?.id ?? ""));
				}
				return data;
			},
			201,
		),
	);
	app.get("/api/sessions/:sid", (c) => answer(c, async () => withBusy(c, await hub.engine.session(sid(c)))));
	app.delete("/api/sessions/:sid", (c) =>
		answer(c, async () => {
			if (hub.busy(sid(c))) throw new ResumesError("RANKING_RUNNING", "The assistant is working — stop it first, or wait until it is done.");
			const gone = await hub.engine.deleteSession(sid(c));
			hub.owners.forget(sid(c));
			return gone;
		}),
	);
	app.get("/api/sessions/:sid/state", (c) => answer(c, async () => withBusy(c, await hub.engine.state(sid(c), c.req.query("overview") !== "0"))));
	app.post("/api/sessions/:sid/action", (c) =>
		answer(c, async () => {
			const data = await bodyOf(c);
			if (hub.busy(sid(c))) {
				const cfg = await hub.engine.config();
				if (cfg.changes.includes(String(data.type))) throw new ResumesError("BUSY", "The assistant is working — stop it first, or wait until it is done.");
			}
			return hub.engine.action(sid(c), data);
		}),
	);
	app.get("/api/sessions/:sid/people/:ref", (c) => answer(c, () => hub.engine.person(sid(c), param(c, "ref"))));
	app.get("/api/sessions/:sid/people/:ref/text", async (c) => {
		const r = await hub.engine.personText(sid(c), param(c, "ref"));
		return new Response(r.body, { status: r.status, headers: { "content-type": r.headers.get("content-type") || "text/plain; charset=utf-8" } });
	});
	app.post("/api/sessions/:sid/stop", (c) => answer(c, async () => ({ stopped: hub.stop(sid(c)) })));

	app.post("/api/sessions/:sid/chat", async (c) => {
		const id = sid(c);
		let data: Record<string, unknown>;
		try {
			data = await bodyOf(c);
		} catch (e) {
			const p = problem(e);
			return c.json(p.body, p.status as 400);
		}
		const text = String(data.text || "").trim();
		const attachment = data.attachment && typeof data.attachment === "object" ? (data.attachment as { name?: string | null; text?: string }) : null;
		const auto = data.auto === "rank_pending" ? "rank_pending" : null;
		if (!text && !attachment && !auto) return c.json({ error: { role: "error", code: "BAD_ARGUMENT", text: "say something" } }, 422);
		if (hub.busy(id)) return c.json({ error: { role: "error", code: "BUSY", text: "The assistant is still working on the last message." } }, 409);
		return streamSSE(c, async (stream) => {
			let chain = Promise.resolve();
			const emit = (ev: Record<string, unknown>) => {
				chain = chain.then(() => stream.writeSSE({ event: String(ev.type), data: JSON.stringify(ev) })).catch(() => {});
			};
			try {
				const user = c.get("user");
				if (text.startsWith("/") && !attachment) {
					const out = await hub.engine.slash(id, text);
					emit({ type: "user", message: out.user });
					if (out.error) emit({ type: "error", message: out.error, state: out.state });
					else if (out.handoff) await hub.turn(id, { text: out.handoff, auto: "handoff" }, emit, user);
					else emit({ type: "done", messages: out.messages, state: out.state, ui: out.ui });
				} else {
					await hub.turn(id, { text, attachment, auto }, emit, user);
				}
			} catch (e) {
				const p = problem(e);
				emit({ type: "error", message: p.body.error, ...(p.body.state ? { state: p.body.state } : {}) });
			}
			await chain;
		});
	});

	app.get("/api/settings", (c) => answer(c, () => hub.settingsView(c.get("user"))));
	app.put("/api/settings", (c) =>
		answer(c, async () => {
			const data = await bodyOf(c);
			await hub.applySettings(data, c.get("user"));
			return hub.settingsView(c.get("user"));
		}),
	);
	app.get("/api/providers/:provider/models", (c) =>
		answer(c, async () => {
			const refresh = ["1", "true", "yes"].includes(c.req.query("refresh") || "");
			if (refresh && hub.limited(c.get("user"))) throw new SignupError("ADMIN_ONLY", 403, "Refreshing the list is for the server's operators.");
			return hub.offeredTo(param(c, "provider"), c.get("user"), refresh);
		}),
	);

	app.get("/api/me", (c) => {
		const login = hub.auth.on();
		const user = login ? hub.auth.who(c) : null;
		// nothing of the collection before the login
		const pages = hub.features.filter((f) => f.page).map((f) => f.name);
		const extra = Object.assign({}, ...hub.features.map((f) => f.me?.(user) ?? {}));
		return c.json({ login, user, ...(hub.signup ? { signup: true } : {}), ...(user ? { role: hub.role(user) } : {}), ...extra, ...(pages.length ? { features: pages } : {}) });
	});
	const featureApi: FeatureApi = {
		answer: (c, fn) => answer(c, fn),
		bodyOf,
		user: (c) => {
			const user = c.get("user");
			if (!user) throw new SignupError("LOGIN_REQUIRED", 401, "Log in first.");
			return user;
		},
	};
	for (const f of hub.features) f.routes?.(app, featureApi);
	app.post("/api/login", async (c) => {
		let data: Record<string, unknown>;
		try {
			data = await bodyOf(c);
		} catch (e) {
			const p = problem(e);
			return c.json(p.body, p.status as 400);
		}
		if (!hub.auth.on()) return c.json({ error: { role: "error", code: "LOGIN_OFF", text: "There is no login on this server: no account was made with `resumes users add`." } }, 422);
		const name = String(data.user ?? data.name ?? "");
		const ip = ipOf(c);
		const wait = Math.max(hub.auth.lockedFor(name), hub.auth.ipLockedFor(ip));
		if (wait) return c.json({ error: { role: "error", code: "LOGIN_LOCKED", text: `Too many wrong passwords — wait ${wait} s and try again.`, retry_in: wait } }, 429);
		const user = hub.auth.login(c, name, String(data.password ?? ""), ip);
		if (!user) return c.json({ error: { role: "error", code: "LOGIN_FAILED", text: "Wrong name or password." } }, 401);
		return c.json({ user });
	});
	app.post("/api/logout", (c) => {
		hub.auth.logout(c);
		return c.json({ user: null });
	});

	// the sign-up: the flow is kept on the server; the cookie holds only its id
	const flowId = (c: Context) => getCookie(c, SIGNUP_COOKIE) || null;
	const keepFlow = (c: Context, id: string) =>
		setCookie(c, SIGNUP_COOKIE, id, { path: "/", httpOnly: true, sameSite: "Lax", secure: isHttps(c), maxAge: hub.policy.signup.flow_ttl });
	const signupRoute = (fn: (c: Context, data: Record<string, unknown>) => Promise<unknown> | unknown) => async (c: Context) => {
		if (!hub.signup) return c.json({ error: { role: "error", code: "SIGNUP_OFF", text: "There is no sign-up on this server: ask the person who runs it for an account." } }, 404);
		try {
			const data = c.req.method === "GET" || c.req.method === "DELETE" ? {} : await bodyOf(c);
			return c.json((await fn(c, data)) as any);
		} catch (e) {
			const p = problem(e);
			if (e instanceof SignupError && e.code === "SIGNUP_EXPIRED") deleteCookie(c, SIGNUP_COOKIE, { path: "/" });
			return c.json(p.body, p.status as 400);
		}
	};
	app.get(
		"/api/signup",
		signupRoute((c) => {
			const id = flowId(c);
			const f = hub.signup!.flow(id);
			if (!f && id) deleteCookie(c, SIGNUP_COOKIE, { path: "/" });
			return hub.signup!.view(f, !!id); // reads only: opening the page never sends a code
		}),
	);
	app.post(
		"/api/signup/email",
		signupRoute(async (c, d) => {
			const { id, view } = await hub.signup!.start(flowId(c), d.email, ipOf(c), siteOf(c));
			keepFlow(c, id);
			return view;
		}),
	);
	app.post("/api/signup/email/verify", signupRoute((c, d) => hub.signup!.verifyEmail(flowId(c), d.code)));
	app.post("/api/signup/phone", signupRoute((c, d) => hub.signup!.phone(flowId(c), d.phone, ipOf(c), siteOf(c))));
	app.delete("/api/signup/phone", signupRoute((c) => hub.signup!.changePhone(flowId(c))));
	app.post("/api/signup/phone/verify", signupRoute((c, d) => hub.signup!.verifyPhone(flowId(c), d.code)));
	app.post("/api/signup/resend", signupRoute((c) => hub.signup!.resend(flowId(c), ipOf(c), siteOf(c))));
	app.post(
		"/api/signup/password",
		signupRoute((c, d) => {
			const name = hub.signup!.finish(flowId(c), d.password, ipOf(c));
			deleteCookie(c, SIGNUP_COOKIE, { path: "/" });
			hub.auth.setLogin(c, name);
			return { user: name, role: hub.role(name) };
		}),
	);
	app.delete(
		"/api/signup",
		signupRoute((c) => {
			hub.signup!.cancel(flowId(c));
			deleteCookie(c, SIGNUP_COOKIE, { path: "/" });
			return { step: "email" };
		}),
	);

	// the UI's files, read from disk on every request: small, local, and edited while the server runs
	app.get("/*", async (c) => {
		const path = normalize(decodeURIComponent(c.req.path)).replace(/^\/+/, "");
		if (path.includes("..")) return c.text("no", 404);
		const file = join(publicDir, path === "" || path.endsWith("/") ? `${path}index.html` : path);
		try {
			const s = await stat(file);
			if (!s.isFile()) return c.text("not found", 404);
			return new Response(await readFile(file), { headers: { "content-type": TYPES[extname(file)] || "application/octet-stream", "cache-control": "no-cache" } });
		} catch {
			return c.text("not found", 404);
		}
	});
	return app;
}

function args(argv: string[]): Record<string, string | boolean> {
	const out: Record<string, string | boolean> = {};
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (!a.startsWith("--")) continue;
		const k = a.slice(2);
		const v = argv[i + 1];
		if (v !== undefined && !v.startsWith("--")) {
			out[k] = v;
			i++;
		} else out[k] = true;
	}
	return out;
}

export async function main(): Promise<void> {
	const a = args(process.argv.slice(2));
	const host = String(a.host || "127.0.0.1");
	const port = Number(a.port || 8765);
	let policy: unknown = {};
	if (typeof a.policy === "string") {
		try {
			policy = JSON.parse(a.policy);
		} catch {
			console.error(`resumes web · --policy is not JSON: ${a.policy}`);
			process.exit(2);
		}
	}
	const opts: HubOptions = {
		engineUrl: String(a.engine || "http://127.0.0.1:8770"),
		stateDir: resolve(String(a["state-dir"] || join(HERE, "..", "..", ".resumes"))),
		faux: !!a.faux || !!process.env.RESUMES_FAUX,
		signup: !!a.signup,
		policy,
	};
	const hub = new Hub(opts);
	hub.features = await loadFeatures(hub);
	const publicHosts = String(a["public-host"] || "")
		.split(",")
		.map((s) => s.trim().toLowerCase())
		.filter(Boolean);
	const app = createApp(hub, new Set([...LOCAL_HOSTS, host.toLowerCase(), ...publicHosts]));
	const server = serve({ fetch: app.fetch, hostname: host, port }, async (info) => {
		const url = `http://${host === "127.0.0.1" || host === "0.0.0.0" ? "localhost" : host}:${info.port}`;
		let line = `resumes web · ${url}${publicHosts.length ? ` · public ${publicHosts.join(", ")}` : ""} · engine ${opts.engineUrl} · pi in-process`;
		try {
			const f = await hub.assistantFields();
			line += ` · assistant ${f.model}${f.assistant ? "" : ` (chat off — ${f.assistant_off}; the panel and the slash commands work)`}`;
		} catch (e) {
			line += ` · engine not reached yet (${(e as Error).message})`;
		}
		const n = hub.auth.users.count();
		line += n ? ` · login on (${n} user${n === 1 ? "" : "s"}${n <= 5 ? `: ${hub.auth.users.names().join(", ")}` : ""})` : hub.signup ? " · login on (no account yet)" : " · login off (no account yet: resumes users add NAME)";
		if (hub.signup) line += ` · signup on (${hub.signup.senders.name}; ${hub.policy.signup.sms_per_day} SMS and ${hub.policy.signup.emails_per_day} emails a day; members: ${hub.policy.limits.turns_per_day} turns and $${hub.policy.limits.usd_per_day} a day)`;
		else if (hub.signupOff) line += ` · signup off (${hub.signupOff})`;
		for (const f of hub.features) line += ` · ${f.startLine?.() ?? f.name}`;
		console.log(line);
		if (a.open) setTimeout(() => exec(`${process.platform === "darwin" ? "open" : "xdg-open"} ${url}`), 600);
	});
	server.on("error", (e: NodeJS.ErrnoException) => {
		console.error(e.code === "EADDRINUSE" ? `resumes web · ${host}:${port} is in use — is another \`resumes web\` running? (stop it, or pass --port)` : `resumes web · ${e.message}`);
		process.exit(1);
	});
	if (a["exit-with-stdin"]) {
		process.stdin.resume();
		process.stdin.on("end", () => process.exit(0));
		process.stdin.on("close", () => process.exit(0));
	}
	for (const sig of ["SIGINT", "SIGTERM"] as const) process.on(sig, () => process.exit(0));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) void main();
