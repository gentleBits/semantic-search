// Login over the API against the real engine.
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { COOKIE, TTL_SECONDS, hashPassword } from "../src/auth.js";
import { Api, type EngineHandle, startEngine } from "./helpers.js";

let engine: EngineHandle | null = null;
before(async () => {
	engine = await startEngine();
});
after(() => engine?.stop());

const DAVID = "dana@example.com";
const COLE = "cole@example.com";

function usersFile(dir: string, names: string[], password = "cretzuel"): string {
	const path = join(dir, "users.json");
	writeFileSync(path, JSON.stringify({ users: Object.fromEntries(names.map((n) => [n, { hash: hashPassword(n === COLE ? password : password), created: "2026-09-30T18:00:00+00:00" }])) }));
	return path;
}

/** The cookie a login answered with, as the browser would send it back. */
function cookieOf(headers: Headers): string {
	const set = headers.get("set-cookie") || "";
	const m = new RegExp(`${COOKIE}=([^;]+)`).exec(set);
	assert.ok(m, `no ${COOKIE} cookie in ${JSON.stringify(set)}`);
	return `${COOKIE}=${m![1]}`;
}

test("no users file: no login, everything as before", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const api = await new Api(engine).init();
	assert.deepEqual((await api.request("GET", "/api/me")).body, { login: false, user: null });
	assert.equal((await api.request("POST", "/api/login", { user: DAVID, password: "cretzuel" })).status, 422, "nothing to log into");
	assert.equal((await api.request("GET", "/api/sessions")).body.sessions.length, 1, "the list is everyone's");
	assert.ok(!existsSync(join(api.stateDir, "owners.json")), "nobody owns anything while login is off");
	assert.ok(!existsSync(join(api.stateDir, "secret")), "no secret is made until a login happens");
});

test("with users: the cookie, the lock, each one's own searches, logout, a removed account, an expired cookie", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const dir = mkdtempSync(join(tmpdir(), "resumes-web-login-"));
	let now = Date.now();
	const api = new Api(engine, dir);
	api.hub.auth.now = () => now;
	// made before the users file exists, so nobody owns it
	const before = (await api.request("POST", "/api/sessions", {})).body.session.id;
	const users = usersFile(dir, [DAVID, COLE]);

	let r = await api.request("GET", "/api/config");
	assert.ok(r.status === 401 && r.body.error.code === "LOGIN_REQUIRED", r.text);
	assert.equal((await api.request("GET", `/api/sessions/${before}/state`)).status, 401);
	assert.deepEqual((await api.request("GET", "/api/me")).body, { login: true, user: null }, "nothing of the collection before the login");
	r = await api.request("GET", "/");
	assert.ok(r.status === 200 && r.text.includes("<title>Semantic search</title>"), "the page itself needs no login");
	assert.equal((await api.request("GET", "/js/login.js")).status, 200);

	r = await api.request("POST", "/api/login", { user: DAVID, password: "nope" });
	assert.ok(r.status === 401 && r.body.error.code === "LOGIN_FAILED" && r.body.error.text === "Wrong name or password.", r.text);
	assert.ok(!r.headers.get("set-cookie"));
	r = await api.request("POST", "/api/login", { user: "nobody@example.com", password: "cretzuel" });
	assert.ok(r.status === 401 && r.body.error.text === "Wrong name or password.", "an unknown name reads the same as a wrong password");
	for (let i = 0; i < 4; i++) await api.request("POST", "/api/login", { user: DAVID, password: `wrong-${i}` });
	r = await api.request("POST", "/api/login", { user: DAVID, password: "cretzuel" });
	assert.ok(r.status === 429 && r.body.error.code === "LOGIN_LOCKED" && /wait \d+ s/.test(r.body.error.text), `five wrong ones lock the name: ${r.text}`);
	assert.equal((await api.request("POST", "/api/login", { user: COLE, password: "cretzuel" })).status, 200, "the lock is the name's, not the server's");
	now += 61_000;

	r = await api.request("POST", "/api/login", { user: " Dana@Example.com ", password: "cretzuel" });
	assert.equal(r.status, 200, r.text);
	assert.deepEqual(r.body, { user: DAVID }, "the name is trimmed and lowercased");
	const set = r.headers.get("set-cookie")!;
	assert.match(set, new RegExp(`^${COOKIE}=[A-Za-z0-9_-]+\\.\\d+\\.[A-Za-z0-9_-]+; Max-Age=${TTL_SECONDS}; Path=/; HttpOnly; SameSite=Lax$`), set);
	assert.ok(!/Secure/.test(set), "plain http: no Secure flag, or the browser would drop it");
	const https = await api.request("POST", "/api/login", { user: DAVID, password: "cretzuel" }, { "x-forwarded-proto": "https" });
	assert.match(https.headers.get("set-cookie")!, /; Secure/, "behind a TLS proxy the cookie is Secure");
	assert.equal(statSync(join(dir, "secret")).mode & 0o777, 0o600);
	const david = cookieOf(r.headers);

	api.headers = { cookie: david };
	assert.deepEqual((await api.request("GET", "/api/me")).body, { login: true, user: DAVID, role: "admin" }, "an older account (no role in the file) is an admin");
	assert.equal((await api.request("GET", "/api/config")).status, 200);
	assert.deepEqual((await api.request("GET", "/api/sessions")).body.sessions, [], "the search from before the login is nobody's");
	r = await api.request("GET", `/api/sessions/${before}`);
	assert.ok(r.status === 404 && r.body.error.code === "UNKNOWN_SESSION", r.text);
	r = await api.request("POST", "/api/sessions", {});
	assert.equal(r.status, 201);
	const davids = r.body.session.id;
	assert.deepEqual(JSON.parse(readFileSync(join(dir, "owners.json"), "utf-8"))[davids].user, DAVID);
	assert.deepEqual((await api.request("GET", "/api/sessions")).body.sessions.map((s: any) => s.id), [davids]);
	api.sid = davids;
	api.script = ["**3,026 people** — everyone."];
	const events = await api.say("how many people are there?");
	assert.equal(events.at(-1)!.type, "done", "a turn runs for the owner");
	assert.equal((await api.act({ type: "page", n: 2 })).status, 200);

	const cole = cookieOf((await api.request("POST", "/api/login", { user: COLE, password: "cretzuel" }, { cookie: "" })).headers);
	api.headers = { cookie: cole };
	assert.deepEqual((await api.request("GET", "/api/sessions")).body.sessions, []);
	for (const [method, path, body] of [
		["GET", `/api/sessions/${davids}`, undefined],
		["GET", `/api/sessions/${davids}/state`, undefined],
		["POST", `/api/sessions/${davids}/action`, { type: "page", n: 1 }],
		["POST", `/api/sessions/${davids}/chat`, { text: "hello" }],
		["GET", `/api/sessions/${davids}/people/1`, undefined],
		["GET", `/api/sessions/${davids}/people/1/text`, undefined],
		["POST", `/api/sessions/${davids}/stop`, {}],
		["DELETE", `/api/sessions/${davids}`, undefined],
	] as [string, string, unknown][]) {
		r = await api.request(method, path, body);
		assert.ok(r.status === 404 && r.body.error.code === "UNKNOWN_SESSION", `${method} ${path}: ${r.status} ${r.text}`);
	}
	const coles = (await api.request("POST", "/api/sessions", {})).body.session.id;
	assert.deepEqual((await api.request("GET", "/api/sessions")).body.sessions.map((s: any) => s.id), [coles]);
	assert.equal((await api.request("DELETE", `/api/sessions/${coles}`)).status, 200, "one's own can be deleted");
	assert.ok(!(coles in JSON.parse(readFileSync(join(dir, "owners.json"), "utf-8"))), "and is forgotten");
	api.headers = { cookie: david };
	assert.deepEqual((await api.request("GET", "/api/sessions")).body.sessions.map((s: any) => s.id), [davids], "david's list is unchanged");

	r = await api.request("POST", "/api/logout", {});
	assert.match(r.headers.get("set-cookie")!, new RegExp(`^${COOKIE}=; Max-Age=0; Path=/`), "the cookie is cleared");
	assert.equal((await api.request("GET", "/api/config", undefined, { cookie: "" })).status, 401);
	assert.equal((await api.request("GET", "/api/config")).status, 200, "an old cookie still held by a tab is still valid until it expires");

	writeFileSync(users, JSON.stringify({ users: { [COLE]: { hash: hashPassword("cretzuel") } } }));
	assert.equal((await api.request("GET", "/api/config")).status, 401, "david was removed from the file");
	api.headers = { cookie: cole };
	assert.equal((await api.request("GET", "/api/config")).status, 200);
	now += (TTL_SECONDS + 1) * 1000;
	r = await api.request("GET", "/api/config");
	assert.ok(r.status === 401 && r.body.error.code === "LOGIN_REQUIRED", "30 days later");
	now -= TTL_SECONDS * 1000;
	const [payload, exp] = cole.split("=")[1].split(".");
	api.headers = { cookie: `${COOKIE}=${payload}.${exp}.forged` };
	assert.equal((await api.request("GET", "/api/config")).status, 401);
	api.headers = { cookie: `${COOKIE}=${Buffer.from(DAVID).toString("base64url")}.${exp}.${cole.split(".")[2]}` };
	assert.equal((await api.request("GET", "/api/config")).status, 401, "another name under cole's signature");

	unlinkSync(users);
	api.headers = {};
	assert.deepEqual((await api.request("GET", "/api/me")).body, { login: false, user: null });
	const all = (await api.request("GET", "/api/sessions")).body.sessions.map((s: any) => s.id);
	assert.ok(all.includes(before) && all.includes(davids), `everyone's again: ${all}`);
});
