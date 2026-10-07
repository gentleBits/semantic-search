// The email and phone check over HTTP against the real engine: routes, cookies, client addresses and limits.
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { hashPassword } from "../src/auth.js";
import { SMS_COUNTRIES } from "../src/policy.js";
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

test("the check off: nothing changes (no route, no word of it in /api/me)", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const api = new Api(engine);
	assert.deepEqual((await api.request("GET", "/api/me")).body, { login: false, user: null });
	const r = await api.request("GET", "/api/signup");
	assert.ok(r.status === 404 && r.body.error.code === "SIGNUP_OFF", r.text);
	assert.equal((await api.request("POST", "/api/signup/start", { email: "ana@example.com", phone: "+40712345678" })).status, 404);
});

const FORM = { email: "Ana@Example.com", phone: "+40 712 345 678" };

test("the check through the API, the first time and the next: the cookies, the steps, in at the end, the same searches", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const dry = new DryRun(false);
	const api = new Api(engine, undefined, { signup: true, senders: dry });
	const b = new Browser(api, "203.0.113.7", { "x-forwarded-proto": "https" });

	assert.deepEqual((await b.get("/api/me")).body, { login: true, user: null, signup: true }, "the check on with nobody yet: nobody is let in unchecked");
	let r = await b.get("/api/config");
	assert.ok(r.status === 401 && r.body.error.text === "Please confirm your email and phone to search.", "nothing of the collection before the check");
	assert.deepEqual((await b.get("/api/signup")).body, { step: "details", work_email: true, countries: SMS_COUNTRIES });

	r = await b.post("/api/signup/start", { ...FORM, email: "ana@gmail.com" });
	assert.ok(r.status === 422 && r.body.error.code === "PERSONAL_EMAIL" && /business email/.test(r.body.error.text) && !b.jar.has(SIGNUP_COOKIE), r.text);
	r = await b.post("/api/signup/start", { ...FORM, phone: "0712 345 678" });
	assert.ok(r.status === 422 && r.body.error.code === "BAD_PHONE" && !b.jar.has(SIGNUP_COOKIE), r.text);
	r = await b.post("/api/signup/start", FORM);
	assert.equal(r.status, 200, r.text);
	assert.deepEqual([r.body.step, r.body.email, r.body.phone], ["email_code", "ana@example.com", "+40 712 345 678"]);
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
	assert.ok(r.body.step === "phone_code" && r.body.phone === "+40 712 345 678", r.text);
	const flowCookie = b.jar.get(SIGNUP_COOKIE)!;
	r = await b.post("/api/signup/phone/verify", { code: codeOf(dry, "sms") });
	assert.deepEqual(r.body, { done: true, user: "ana@example.com", role: "member" }, r.text);
	assert.ok(b.jar.has("resumes_login") && !b.jar.has(SIGNUP_COOKIE), "in; the flow's cookie is gone");
	assert.deepEqual((await b.get("/api/me")).body, { login: true, user: "ana@example.com", signup: true, role: "member" });
	assert.equal((await b.get("/api/config")).status, 200);
	r = await b.post("/api/sessions", {});
	assert.equal(r.status, 201, r.text);
	const sid = r.body.session.id;
	assert.deepEqual((await b.get("/api/sessions")).body.sessions.map((s: any) => s.id), [sid], "her own first search");

	const replay = new Browser(api);
	replay.jar.set(SIGNUP_COOKIE, flowCookie);
	r = await replay.post("/api/signup/phone/verify", { code: codeOf(dry, "sms") });
	assert.ok(r.status === 410 && r.body.error.code === "SIGNUP_EXPIRED", r.text);
	assert.ok(!replay.jar.has(SIGNUP_COOKIE) && !replay.jar.has("resumes_login"), "and the stale cookie is cleared");
	assert.equal((await replay.post("/api/login", { user: "ana@example.com", password: "" })).status, 401, "no password to log in with");

	// another browser, the next day: the same form, the email's code alone, the same searches
	const texts = dry.sent.filter((m) => m.kind === "sms").length;
	const other = new Browser(api, "198.51.100.4");
	r = await other.post("/api/signup/start", { email: "ana@example.com", phone: "0040712345678" });
	assert.equal(r.body.step, "email_code", r.text);
	r = await other.post("/api/signup/email/verify", { code: codeOf(dry, "email") });
	assert.deepEqual(r.body, { done: true, user: "ana@example.com", role: "member" }, r.text);
	assert.equal(dry.sent.filter((m) => m.kind === "sms").length, texts, "no text the second time");
	assert.deepEqual((await other.get("/api/sessions")).body.sessions.map((s: any) => s.id), [sid], "her search, from another browser");

	// "Forget this browser": the check again
	assert.equal((await other.post("/api/logout")).status, 200);
	assert.equal((await other.get("/api/config")).status, 401);
	assert.deepEqual((await other.get("/api/signup")).body, { step: "details", work_email: true, countries: SMS_COUNTRIES });
});

test("breaking it over HTTP: steps out of order, no cookie, a guessed cookie, another site, a forged address", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const dry = new DryRun(false);
	const api = new Api(engine, undefined, { signup: true, senders: dry });
	const b = new Browser(api);
	await b.post("/api/signup/start", FORM);
	let r = await b.post("/api/signup/phone/verify", { code: "123456" });
	assert.ok(r.status === 409 && r.body.error.code === "OUT_OF_ORDER" && r.body.error.view.step === "email_code", r.text);
	r = await b.post("/api/signup/phone", { phone: "+40712345679" });
	assert.ok(r.status === 409 && r.body.error.code === "OUT_OF_ORDER", r.text);
	assert.equal((await b.post("/api/signup/password", { password: "a long password" })).status, 404, "no password step any more");

	const nobody = new Browser(api, "198.51.100.9");
	r = await nobody.post("/api/signup/email/verify", { code: "123456" });
	assert.ok(r.status === 410 && r.body.error.code === "SIGNUP_EXPIRED", "no cookie: no flow");
	nobody.jar.set(SIGNUP_COOKIE, "A".repeat(43));
	r = await nobody.post("/api/signup/phone/verify", { code: "123456" });
	assert.equal(r.status, 410, "a guessed id is no flow");

	r = await b.req("POST", "/api/signup/email/verify", { code: codeOf(dry, "email") }, { origin: "https://evil.example" });
	assert.equal(r.status, 403, "another site's page cannot post");
	r = await b.req("POST", "/api/signup/start", "email=ana@example.com&phone=%2B40712345678", { "content-type": "application/x-www-form-urlencoded" });
	assert.equal(r.status, 403, "a form post from elsewhere is not JSON");
	assert.equal((await b.post("/api/signup/email/verify", { code: codeOf(dry, "email") })).body.step, "phone_code", "the page itself goes on");
	r = await b.req("DELETE", "/api/signup/phone");
	assert.equal(r.body.step, "phone", "use another number");
	assert.equal((await b.get("/api/signup")).body.step, "phone", "a refresh stays there");
	r = await b.req("DELETE", "/api/signup");
	assert.deepEqual(r.body, { step: "details", work_email: true, countries: SMS_COUNTRIES }, "start over");
	assert.ok(!b.jar.has(SIGNUP_COOKIE));

	// X-Forwarded-For counts only from a loopback peer (the proxy), and only its last entry
	assert.equal(clientIp("127.0.0.1", "203.0.113.7"), "203.0.113.7");
	assert.equal(clientIp("::1", "10.0.0.1, 203.0.113.7"), "203.0.113.7", "the entry Caddy added");
	assert.equal(clientIp("::ffff:127.0.0.1", "203.0.113.7"), "203.0.113.7");
	assert.equal(clientIp("198.51.100.20", "203.0.113.7"), "198.51.100.20", "a direct visitor cannot choose its address");
	assert.equal(clientIp("::ffff:198.51.100.20", null), "198.51.100.20");
	assert.equal(clientIp(undefined, undefined), "127.0.0.1");

	// 10 new flows an hour per address
	const form = (email: string) => ({ email, phone: "+40712345678" });
	const many = new Browser(api, "192.0.2.50");
	for (let i = 0; i < 10; i++) assert.equal((await many.post("/api/signup/start", form(`m${i}@example.com`))).status, 200);
	r = await many.post("/api/signup/start", form("m10@example.com"));
	assert.ok(r.status === 429 && r.body.error.code === "LIMIT" && r.body.error.retry_in > 3000, r.text);
	assert.equal((await new Browser(api, "192.0.2.51").post("/api/signup/start", form("m10@example.com"))).status, 200);

	// IPv6: one /64 counts as one address for the limits
	for (let i = 1; i <= 10; i++) assert.equal((await new Browser(api, `2001:db8:1:2::${i.toString(16)}`).post("/api/signup/start", form(`v${i}@example.com`))).status, 200);
	r = await new Browser(api, "2001:db8:1:2:ffff:ffff:ffff:fffe").post("/api/signup/start", form("v11@example.com"));
	assert.ok(r.status === 429 && r.body.error.code === "LIMIT", r.text);
	assert.equal((await new Browser(api, "2001:db8:1:3::1").post("/api/signup/start", form("v12@example.com"))).status, 200, "the next /64 is someone else");
});

test("an operator's address needs no text; a blocked person is out at once and their number refused; wrong passwords from one address", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const dry = new DryRun(false);
	let now = Date.now();
	const api = new Api(engine, undefined, { signup: true, senders: dry, now: () => now });
	writeFileSync(
		join(api.stateDir, "users.json"),
		JSON.stringify({
			users: {
				"dana@example.com": { hash: hashPassword("cretzuel"), created: "2026-09-30T18:00:00+00:00" },
				"ana@example.com": { created: "2026-10-01T10:00:00+00:00", role: "member", phone: "+40712345678", phones: ["+40712345678"], via: "check" },
			},
		}),
	);
	const dana = new Browser(api, "198.51.100.1");
	await dana.post("/api/signup/start", { email: "dana@example.com", phone: "+49 151 23456789" });
	let r = await dana.post("/api/signup/email/verify", { code: codeOf(dry, "email") });
	assert.deepEqual(r.body, { done: true, user: "dana@example.com", role: "admin" }, r.text);
	assert.equal(dry.sent.filter((m) => m.kind === "sms").length, 0);

	// a flow lasts 30 minutes
	const slow = new Browser(api, "198.51.100.3");
	await slow.post("/api/signup/start", { email: "slow@example.com", phone: "+40712345600" });
	now += 31 * 60_000;
	assert.deepEqual((await slow.get("/api/signup")).body, { step: "details", expired: true, work_email: true, countries: SMS_COUNTRIES });
	assert.ok(!slow.jar.has(SIGNUP_COOKIE));

	const ana = new Browser(api, "198.51.100.4");
	await ana.post("/api/signup/start", { email: "ana@example.com", phone: "+40712345678" });
	assert.equal((await ana.post("/api/signup/email/verify", { code: codeOf(dry, "email") })).body.done, true);
	assert.equal((await ana.get("/api/config")).status, 200);
	const raw = JSON.parse(readFileSync(join(api.stateDir, "users.json"), "utf-8"));
	raw.users["ana@example.com"].role = "blocked";
	writeFileSync(join(api.stateDir, "users.json"), JSON.stringify(raw));
	assert.equal((await ana.get("/api/config")).status, 401, "blocked: out at once");
	const back = new Browser(api, "198.51.100.5");
	await back.post("/api/signup/start", { email: "ana.again@example.com", phone: "+40712345678" });
	r = await back.post("/api/signup/email/verify", { code: codeOf(dry, "email") });
	assert.ok(r.status === 403 && r.body.error.code === "PHONE_BLOCKED" && r.body.error.view.step === "phone", r.text);

	// 20 wrong passwords from one address, whatever the names, lock that address
	const guesser = new Browser(api, "192.0.2.66");
	for (let i = 0; i < 20; i++) assert.equal((await guesser.post("/api/login", { user: `u${i}@example.com`, password: "guess" })).status, 401);
	r = await guesser.post("/api/login", { user: "dana@example.com", password: "cretzuel" });
	assert.ok(r.status === 429 && r.body.error.code === "LOGIN_LOCKED" && r.body.error.retry_in > 500, r.text);
	assert.equal((await new Browser(api, "192.0.2.67").post("/api/login", { user: "dana@example.com", password: "cretzuel" })).status, 200);
	now += 11 * 60_000;
	r = await guesser.post("/api/login", { user: "dana@example.com", password: "cretzuel" });
	assert.equal(r.status, 200, "ten minutes later");
});
