// Sign-up over HTTP against the real engine: routes, cookies, client addresses and limits.
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { hashPassword } from "../src/auth.js";
import { DryRun } from "../src/senders.js";
import { SIGNUP_COOKIE, clientIp } from "../src/server.js";
import { Api, type EngineHandle, startEngine } from "./helpers.js";

let engine: EngineHandle | null = null;
before(async () => {
	engine = await startEngine();
});
after(() => engine?.stop());

/** One browser: its own cookies and its own address, sent as X-Forwarded-For. */
class Browser {
	jar = new Map<string, string>();
	setCookies: string[] = [];
	constructor(public api: Api, public ip = "203.0.113.7", public extra: Record<string, string> = {}) {}

	async req(method: string, path: string, body?: unknown, headers: Record<string, string> = {}) {
		const cookie = [...this.jar].map(([k, v]) => `${k}=${v}`).join("; ");
		const r = await this.api.request(method, path, body, { ...(cookie ? { cookie } : {}), "x-forwarded-for": this.ip, ...this.extra, ...headers });
		this.setCookies = r.headers.getSetCookie();
		for (const sc of this.setCookies) {
			const [kv] = sc.split(";");
			const at = kv.indexOf("=");
			const k = kv.slice(0, at).trim();
			const v = kv.slice(at + 1).trim();
			if (!v || /max-age=0/i.test(sc)) this.jar.delete(k);
			else this.jar.set(k, v);
		}
		return r;
	}
	get = (path: string) => this.req("GET", path);
	post = (path: string, body: unknown = {}) => this.req("POST", path, body);
}

const codeOf = (dry: DryRun, kind: "email" | "sms") => /\b(\d{6})\b/.exec([...dry.sent].reverse().find((m) => m.kind === kind)!.text)![1];

test("sign-up off: nothing changes (no route, no word of it in /api/me)", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const api = new Api(engine);
	assert.deepEqual((await api.request("GET", "/api/me")).body, { login: false, user: null });
	const r = await api.request("GET", "/api/signup");
	assert.ok(r.status === 404 && r.body.error.code === "SIGNUP_OFF", r.text);
	assert.equal((await api.request("POST", "/api/signup/email", { email: "ana@example.com" })).status, 404);
});

test("the whole sign-up through the API: the cookies, the steps, in at the end, a member's own first search", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const dry = new DryRun(false);
	const api = new Api(engine, undefined, { signup: true, senders: dry });
	const b = new Browser(api, "203.0.113.7", { "x-forwarded-proto": "https" });

	assert.deepEqual((await b.get("/api/me")).body, { login: true, user: null, signup: true }, "sign-up on with no account yet: login is on");
	assert.equal((await b.get("/api/config")).status, 401, "nothing of the collection before an account");
	assert.deepEqual((await b.get("/api/signup")).body, { step: "email", work_email: true });

	let r = await b.post("/api/signup/email", { email: "ana@gmail.com" });
	assert.ok(r.status === 422 && r.body.error.code === "PERSONAL_EMAIL" && /business email/.test(r.body.error.text) && !b.jar.has(SIGNUP_COOKIE), r.text);
	r = await b.post("/api/signup/email", { email: "Ana@Example.com" });
	assert.equal(r.status, 200, r.text);
	assert.equal(r.body.step, "email_code");
	const sc = b.setCookies.find((c) => c.startsWith(`${SIGNUP_COOKIE}=`))!;
	assert.match(sc, /HttpOnly/);
	assert.match(sc, /SameSite=Lax/);
	assert.match(sc, /Secure/, "over https (the proxy says so)");
	assert.match(sc, /Max-Age=1800/);
	assert.match(sc, /Path=\//);
	assert.equal(b.jar.get(SIGNUP_COOKIE)!.length, 43, "the cookie is the flow's random id, nothing else");

	const sends = dry.sent.length;
	for (let i = 0; i < 3; i++) assert.equal((await b.get("/api/signup")).body.step, "email_code", "a refresh lands on the same step");
	assert.equal(dry.sent.length, sends, "opening the page sends nothing");

	r = await b.post("/api/signup/email/verify", { code: codeOf(dry, "email") });
	assert.equal(r.body.step, "phone", r.text);
	r = await b.post("/api/signup/phone", { phone: "+40 712 345 678" });
	assert.ok(r.status === 200 && r.body.step === "phone_code" && r.body.phone === "+40 712 345 678", r.text);
	r = await b.post("/api/signup/phone/verify", { code: codeOf(dry, "sms") });
	assert.equal(r.body.step, "password", r.text);
	const flowCookie = b.jar.get(SIGNUP_COOKIE)!;

	r = await b.post("/api/signup/password", { password: "a long password" });
	assert.deepEqual(r.body, { user: "ana@example.com", role: "member" }, r.text);
	assert.ok(b.jar.has("resumes_login") && !b.jar.has(SIGNUP_COOKIE), "logged in; the flow's cookie is gone");
	assert.deepEqual((await b.get("/api/me")).body, { login: true, user: "ana@example.com", signup: true, role: "member" });
	assert.equal((await b.get("/api/config")).status, 200);
	r = await b.post("/api/sessions", {});
	assert.equal(r.status, 201, r.text);
	assert.deepEqual((await b.get("/api/sessions")).body.sessions.map((s: any) => s.id), [r.body.session.id], "the new member's own search");

	const replay = new Browser(api);
	replay.jar.set(SIGNUP_COOKIE, flowCookie);
	r = await replay.post("/api/signup/password", { password: "another password" });
	assert.ok(r.status === 410 && r.body.error.code === "SIGNUP_EXPIRED", r.text);
	assert.ok(!replay.jar.has(SIGNUP_COOKIE), "and the stale cookie is cleared");
	assert.ok(api.hub.auth.users.verify("ana@example.com", "a long password"));

	const other = new Browser(api, "198.51.100.4");
	assert.equal((await other.post("/api/login", { user: "ana@example.com", password: "a long password" })).status, 200);
	assert.equal((await other.get("/api/sessions")).body.sessions.length, 1, "her search, from another browser");
});

test("breaking it over HTTP: steps out of order, no cookie, a guessed cookie, another site, a forged address", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const dry = new DryRun(false);
	const api = new Api(engine, undefined, { signup: true, senders: dry });
	const b = new Browser(api);
	await b.post("/api/signup/email", { email: "ana@example.com" });
	let r = await b.post("/api/signup/password", { password: "a long password" });
	assert.ok(r.status === 409 && r.body.error.code === "OUT_OF_ORDER", r.text);
	r = await b.post("/api/signup/phone", { phone: "+40712345678" });
	assert.ok(r.status === 409 && r.body.error.code === "OUT_OF_ORDER", r.text);

	const nobody = new Browser(api, "198.51.100.9");
	r = await nobody.post("/api/signup/email/verify", { code: "123456" });
	assert.ok(r.status === 410 && r.body.error.code === "SIGNUP_EXPIRED", "no cookie: no flow");
	nobody.jar.set(SIGNUP_COOKIE, "A".repeat(43));
	r = await nobody.post("/api/signup/phone/verify", { code: "123456" });
	assert.equal(r.status, 410, "a guessed id is no flow");

	r = await b.req("POST", "/api/signup/email/verify", { code: codeOf(dry, "email") }, { origin: "https://evil.example" });
	assert.equal(r.status, 403, "another site's page cannot post");
	r = await b.req("POST", "/api/signup/email", "email=ana@example.com", { "content-type": "application/x-www-form-urlencoded" });
	assert.equal(r.status, 403, "a form post from elsewhere is not JSON");
	assert.equal((await b.post("/api/signup/email/verify", { code: codeOf(dry, "email") })).body.step, "phone", "the page itself goes on");

	// X-Forwarded-For counts only from a loopback peer (the proxy), and only its last entry
	assert.equal(clientIp("127.0.0.1", "203.0.113.7"), "203.0.113.7");
	assert.equal(clientIp("::1", "10.0.0.1, 203.0.113.7"), "203.0.113.7", "the entry Caddy added");
	assert.equal(clientIp("::ffff:127.0.0.1", "203.0.113.7"), "203.0.113.7");
	assert.equal(clientIp("198.51.100.20", "203.0.113.7"), "198.51.100.20", "a direct visitor cannot choose its address");
	assert.equal(clientIp("::ffff:198.51.100.20", null), "198.51.100.20");
	assert.equal(clientIp(undefined, undefined), "127.0.0.1");

	// 10 new flows an hour per address
	const many = new Browser(api, "192.0.2.50");
	for (let i = 0; i < 10; i++) assert.equal((await many.post("/api/signup/email", { email: `m${i}@example.com` })).status, 200);
	r = await many.post("/api/signup/email", { email: "m10@example.com" });
	assert.ok(r.status === 429 && r.body.error.code === "LIMIT" && r.body.error.retry_in > 3000, r.text);
	assert.equal((await new Browser(api, "192.0.2.51").post("/api/signup/email", { email: "m10@example.com" })).status, 200);

	// IPv6: one /64 counts as one address for the limits
	for (let i = 1; i <= 10; i++) assert.equal((await new Browser(api, `2001:db8:1:2::${i.toString(16)}`).post("/api/signup/email", { email: `v${i}@example.com` })).status, 200);
	r = await new Browser(api, "2001:db8:1:2:ffff:ffff:ffff:fffe").post("/api/signup/email", { email: "v11@example.com" });
	assert.ok(r.status === 429 && r.body.error.code === "LIMIT", r.text);
	assert.equal((await new Browser(api, "2001:db8:1:3::1").post("/api/signup/email", { email: "v12@example.com" })).status, 200, "the next /64 is someone else");
});

test("an address with an account answers like a new one; a blocked member is out at once; wrong passwords from one address", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const dry = new DryRun(false);
	let now = Date.now();
	const api = new Api(engine, undefined, { signup: true, senders: dry, now: () => now });
	writeFileSync(
		join(api.stateDir, "users.json"),
		JSON.stringify({
			users: {
				"dana@example.com": { hash: hashPassword("cretzuel"), created: "2026-09-30T18:00:00+00:00" },
				"ana@example.com": { hash: hashPassword("a long password"), created: "2026-10-01T10:00:00+00:00", role: "member", phone: "+40712345678" },
			},
		}),
	);
	const known = await new Browser(api, "198.51.100.1").post("/api/signup/email", { email: "dana@example.com" });
	const fresh = await new Browser(api, "198.51.100.2").post("/api/signup/email", { email: "new@example.com" });
	assert.equal(known.status, fresh.status);
	assert.deepEqual(Object.keys(known.body).sort(), Object.keys(fresh.body).sort());
	assert.deepEqual({ ...known.body, email: "" }, { ...fresh.body, email: "" });

	// a flow lasts 30 minutes
	const slow = new Browser(api, "198.51.100.3");
	await slow.post("/api/signup/email", { email: "slow@example.com" });
	now += 31 * 60_000;
	assert.deepEqual((await slow.get("/api/signup")).body, { step: "email", expired: true, work_email: true });
	assert.ok(!slow.jar.has(SIGNUP_COOKIE));

	const ana = new Browser(api, "198.51.100.4");
	assert.equal((await ana.post("/api/login", { user: "ana@example.com", password: "a long password" })).status, 200);
	assert.equal((await ana.get("/api/config")).status, 200);
	const raw = JSON.parse(readFileSync(join(api.stateDir, "users.json"), "utf-8"));
	raw.users["ana@example.com"].role = "blocked";
	writeFileSync(join(api.stateDir, "users.json"), JSON.stringify(raw));
	assert.equal((await ana.get("/api/config")).status, 401, "blocked: out at once");
	assert.equal((await ana.post("/api/login", { user: "ana@example.com", password: "a long password" })).status, 401);

	// 20 wrong passwords from one address, whatever the names, lock that address
	const guesser = new Browser(api, "192.0.2.66");
	for (let i = 0; i < 20; i++) assert.equal((await guesser.post("/api/login", { user: `u${i}@example.com`, password: "guess" })).status, 401);
	let r = await guesser.post("/api/login", { user: "dana@example.com", password: "cretzuel" });
	assert.ok(r.status === 429 && r.body.error.code === "LOGIN_LOCKED" && r.body.error.retry_in > 500, r.text);
	assert.equal((await new Browser(api, "192.0.2.67").post("/api/login", { user: "dana@example.com", password: "cretzuel" })).status, 200);
	now += 11 * 60_000;
	r = await guesser.post("/api/login", { user: "dana@example.com", password: "cretzuel" });
	assert.equal(r.status, 200, "ten minutes later");
});
