// The email and phone check without HTTP: the users file, the flow's rules, the providers, phones, addresses and the policy.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { Auth, Users, hashPassword } from "../src/auth.js";
import { ResumesError } from "../src/errors.js";
import { withLock } from "../src/files.js";
import { MailDomains, type MxAnswer, listed, mailLists } from "../src/mail-domains.js";
import { formatPhone, parsePhone } from "../src/phone.js";
import { SIGNUP_DEFAULTS, type SignupPolicy, policyOf } from "../src/policy.js";
import { DryRun, type Http, Resend, Sakari, SendError, type Senders, sendersFrom } from "../src/senders.js";
import { type Done, Signup, SignupError, type View, ipKey } from "../src/signup.js";
import { ROOT } from "./helpers.js";

const PYTHON = join(ROOT, ".venv", "bin", "python");
const SITE = { host: "search.example.test", https: true };
const IP = "203.0.113.7";

/** A sign-up on a fresh state dir, a clock the test moves, the dry-run sender (or another). */
function rig(opts: { policy?: Partial<SignupPolicy>; senders?: Senders; dir?: string; t?: { now: number }; mx?: (d: string) => Promise<MxAnswer> } = {}) {
	const dir = opts.dir || mkdtempSync(join(tmpdir(), "resumes-signup-"));
	const clock = opts.t || { now: Date.UTC(2026, 9, 1, 10, 0, 0) };
	const users = new Users(join(dir, "users.json"));
	const sent = opts.senders || new DryRun(false);
	const policy = { ...SIGNUP_DEFAULTS, ...(opts.policy || {}) };
	const s = new Signup(dir, users, sent, () => policy, () => Buffer.alloc(32, 7), () => clock.now, new MailDomains(opts.mx ?? null));
	const later = (seconds: number) => (clock.now += seconds * 1000);
	const dry = sent instanceof DryRun ? sent : null;
	const lastCode = (kind: "email" | "sms") => {
		const m = [...(dry?.sent || [])].reverse().find((x) => x.kind === kind);
		const c = m && /\b(\d{6})\b/.exec(m.text);
		assert.ok(c, `no ${kind} code was sent`);
		return c![1];
	};
	return { dir, clock, users, s, later, dry, lastCode, policy };
}

async function rejects(p: Promise<unknown> | (() => unknown), code: string, what = ""): Promise<SignupError> {
	try {
		await (typeof p === "function" ? p() : p);
	} catch (e) {
		assert.ok(e instanceof SignupError, `${what}: not a SignupError but ${(e as Error)?.message}`);
		assert.equal((e as SignupError).code, code, `${what}: ${(e as Error).message}`);
		return e as SignupError;
	}
	assert.fail(`${what}: expected ${code}, nothing was thrown`);
}

const PHONE = "+40712345678";

/** The form sent and the email's code given: the answer (a text sent, or in) and the flow's id. */
async function throughEmail(r: ReturnType<typeof rig>, email = "ana@example.com", phone = PHONE, ip = IP) {
	const { id } = await r.s.start(null, { email, phone }, ip);
	const out = await r.s.verifyEmail(id, r.lastCode("email"), ip, SITE);
	return { id, out };
}

/** The whole check the first time: email code, text code, in. */
async function firstTime(r: ReturnType<typeof rig>, email = "ana@example.com", phone = PHONE, ip = IP) {
	const { id, out } = await throughEmail(r, email, phone, ip);
	assert.equal((out as View).step, "phone_code", "a new number gets a text");
	return r.s.verifyPhone(id, r.lastCode("sms"), ip) as Done;
}

const smsCount = (r: ReturnType<typeof rig>) => r.dry!.sent.filter((m) => m.kind === "sms").length;
const readUsers = (dir: string) => JSON.parse(readFileSync(join(dir, "users.json"), "utf-8")).users;

test("the users file: members without a password, the numbers they came with, an operator's account kept, blocked people and their numbers", () => {
	const dir = mkdtempSync(join(tmpdir(), "resumes-users-"));
	const path = join(dir, "users.json");
	writeFileSync(path, JSON.stringify({ users: { "dana@example.com": { hash: hashPassword("cretzuel"), created: "2026-09-30T18:00:00+00:00", custom: 42 }, "nopw@example.com": { created: "x" } } }));
	const users = new Users(path);
	assert.equal(users.role("dana@example.com"), "admin", "no role in the file: an admin");
	assert.equal(users.taken("nopw@example.com"), false, "an operator's account without a password is nothing");
	const at = new Date(Date.UTC(2026, 9, 6, 10, 0, 0));
	assert.deepEqual(users.pass("Ana@Example.com", { phone: PHONE, texted: true }, at), { name: "ana@example.com", made: true });
	let raw = readUsers(dir);
	assert.equal(raw["dana@example.com"].custom, 42, "another record's fields are kept");
	assert.deepEqual({ ...raw["ana@example.com"] }, {
		created: "2026-10-06T10:00:00+00:00", role: "member", via: "check", phone: PHONE, phones: [PHONE],
		verified: { email: "2026-10-06T10:00:00+00:00", phone: "2026-10-06T10:00:00+00:00" }, seen: "2026-10-06T10:00:00+00:00",
	}, "no password");
	assert.equal(statSync(path).mode & 0o777, 0o600);
	assert.ok(users.has("ana@example.com") && users.role("ana@example.com") === "member" && users.knownPhone(PHONE));
	assert.ok(!users.verify("ana@example.com", ""), "no password: the login route lets nobody in by it");

	// the second time, with another number that needed no text: the record follows, the first number stays known
	const later = new Date(Date.UTC(2026, 9, 7, 10, 0, 0));
	assert.deepEqual(users.pass("ana@example.com", { phone: "+40712345679", texted: false }, later), { name: "ana@example.com", made: false });
	raw = readUsers(dir);
	assert.equal(raw["ana@example.com"].phone, "+40712345679");
	assert.deepEqual(raw["ana@example.com"].phones, [PHONE, "+40712345679"]);
	assert.deepEqual(raw["ana@example.com"].verified, { email: "2026-10-07T10:00:00+00:00", phone: "2026-10-06T10:00:00+00:00" }, "the phone's date is the text's");
	assert.ok(users.knownPhone(PHONE) && users.knownPhone("+40712345679"));
	users.pass("bob@example.com", { phone: "+40712345679", texted: false }, later);
	assert.deepEqual(users.sharing("ana@example.com").sort(), ["ana@example.com", "bob@example.com"], "one number, one allowance");
	assert.deepEqual(users.sharing("dana@example.com"), ["dana@example.com"]);

	// an operator's address: role, password and numbers as they were
	users.pass("dana@example.com", { phone: "+4915123456789", texted: false }, later);
	raw = readUsers(dir);
	assert.ok(users.verify("dana@example.com", "cretzuel") && users.role("dana@example.com") === "admin");
	assert.equal(raw["dana@example.com"].phone, undefined, "an operator's number is not recorded: it was never texted");
	assert.ok(!users.knownPhone("+4915123456789"));

	// what `resumes users block` does to the file
	const auth = new Auth(dir);
	const token = auth.token("ana@example.com");
	assert.equal(auth.read(token), "ana@example.com");
	raw["ana@example.com"].role = "blocked";
	writeFileSync(path, JSON.stringify({ users: raw }));
	assert.equal(auth.read(token), null, "a blocked person's cookie is refused at once");
	assert.ok(!users.has("ana@example.com") && users.taken("ana@example.com"));
	assert.ok(users.blockedPhone(PHONE) && users.blockedPhone("+40712345679"), "every number the blocked person came with");
	assert.ok(!users.knownPhone("+40712345679"), "… even one someone else came with too");
	assert.throws(() => users.pass("ana@example.com", { phone: "+40712345600", texted: true }), (e: any) => e instanceof ResumesError && e.code === "BLOCKED");
	assert.ok(!existsSync(join(dir, "users.lock")), "the lock is released");
	assert.equal(users.count(), 3, "login stays on");
});

test("the lock is shared with `resumes users` (Python): each waits for the other, nothing is lost", { skip: !existsSync(PYTHON) && "no .venv" }, async () => {
	const dir = mkdtempSync(join(tmpdir(), "resumes-lock-"));
	const path = join(dir, "users.json");
	const users = new Users(path);
	// Python holds the lock for 0.6 s; the write here must wait for it
	const holder = spawn(PYTHON, ["-c", `
import time, sys
from pathlib import Path
from agentic_search.web import users
with users.locked(Path(${JSON.stringify(path)})):
    print("held", flush=True)
    time.sleep(0.6)
`], { cwd: ROOT, stdio: ["ignore", "pipe", "inherit"] });
	await new Promise<void>((ok) => holder.stdout!.once("data", () => ok()));
	const t0 = Date.now();
	users.pass("ana@example.com", { phone: PHONE, texted: true });
	assert.ok(Date.now() - t0 >= 400, `waited for Python's lock (${Date.now() - t0} ms)`);
	await new Promise((ok) => holder.on("exit", ok));
	// Python rewrites the file: a member without a password, and fields it does not know, must survive
	const py = spawnSync(PYTHON, ["-c", `
from pathlib import Path
from agentic_search.web import users
users.add(Path(${JSON.stringify(path)}), "dana@example.com", "cretzuel")
print(sorted(users.load(Path(${JSON.stringify(path)}))))
`], { cwd: ROOT, encoding: "utf-8" });
	assert.equal(py.status, 0, py.stderr);
	assert.equal(py.stdout.trim(), "['ana@example.com', 'dana@example.com']");
	const raw = readUsers(dir);
	assert.deepEqual(raw["ana@example.com"].phones, [PHONE], "Python's rewrite kept the member, numbers and all");
	assert.ok(users.knownPhone(PHONE) && users.role("dana@example.com") === "admin");
	assert.throws(() => withLock(join(dir, "users.lock"), () => withLock(join(dir, "users.lock"), () => 1, 100)), (e: any) => e.code === "BUSY", "a lock held past the wait: refused");
});

test("the first time: the form, the email's code, a text to the new number, its code → in; the flow used up, the ledger written", async () => {
	const r = rig();
	const { id, view } = await r.s.start(null, { email: "  Ana@Example.COM ", phone: "0040 712 345 678" }, IP);
	assert.deepEqual({ ...view, expires_in: 0 }, { step: "email_code", email: "ana@example.com", phone: "+40 712 345 678", expires_in: 0, resend_in: 60, sends_left: 2 });
	assert.equal(r.dry!.sent.length, 1, "only the email: nothing goes to the number before the address is proved");
	const mail = r.dry!.sent[0];
	assert.match(mail.subject!, /^\d{6} is your Semantic search code$/);
	assert.ok(!/https?:/.test(mail.text), "no link in the code email");
	assert.ok(!/account|sign.?up|log ?in/i.test(mail.text + mail.html), "no word of an account");
	const flows = readFileSync(join(r.dir, "signups.json"), "utf-8");
	assert.ok(!flows.includes(r.lastCode("email")), "the code is not in the file, only its HMAC");
	assert.equal(statSync(join(r.dir, "signups.json")).mode & 0o777, 0o600);

	const v = (await r.s.verifyEmail(id, r.lastCode("email"), IP, SITE)) as View;
	assert.equal(v.step, "phone_code");
	assert.equal(v.phone, "+40 712 345 678");
	assert.equal(((await r.s.verifyEmail(id, "000000", IP, SITE)) as View).step, "phone_code", "a double submit after success: no error, no second text");
	assert.equal(smsCount(r), 1);
	const sms = r.dry!.sent.at(-1)!;
	assert.equal(sms.to, PHONE);
	assert.match(sms.text, /^\d{6} is your Semantic search code\.\n\n@search\.example\.test #\d{6}$/, "the origin-bound line phones fill in from");
	assert.deepEqual(r.s.verifyPhone(id, r.lastCode("sms"), IP), { done: true, user: "ana@example.com" });
	assert.equal(r.users.role("ana@example.com"), "member");
	assert.ok(r.users.knownPhone(PHONE));
	assert.equal(r.s.flow(id), null, "the flow is used up");
	await rejects(() => r.s.verifyPhone(id, r.lastCode("sms"), IP), "SIGNUP_EXPIRED", "a replay of the last step");
	const ledger = readFileSync(join(r.dir, "signup-log.jsonl"), "utf-8").trim().split("\n").map((l) => JSON.parse(l));
	assert.deepEqual(ledger.map((l) => l.kind), ["flow", "email", "sms", "passed"]);
	assert.ok(ledger.every((l) => l.ip === IP) && ledger[1].ok === true && ledger[2].to === PHONE);
	assert.deepEqual([ledger[3].to, ledger[3].phone, ledger[3].texted, ledger[3].made], ["ana@example.com", PHONE, true, true]);
	assert.ok(!JSON.stringify(ledger).includes(r.lastCode("sms")), "no code in the ledger");
});

test("the next time: a number that came through before needs no text, whatever the address; an operator's address needs none; a new number does", async () => {
	const r = rig();
	await firstTime(r);
	r.later(3600);
	let { out } = await throughEmail(r, "ana@example.com", "+40 712 345 678");
	assert.deepEqual(out, { done: true, user: "ana@example.com" }, "the email's code alone");
	r.later(3600);
	({ out } = await throughEmail(r, "ana.popescu@example.com", PHONE));
	assert.deepEqual(out, { done: true, user: "ana.popescu@example.com" }, "another address with a known number: in too");
	assert.equal(smsCount(r), 1, "one text in all");
	assert.deepEqual(r.users.sharing("ana@example.com").sort(), ["ana.popescu@example.com", "ana@example.com"]);
	const passed = readFileSync(join(r.dir, "signup-log.jsonl"), "utf-8").trim().split("\n").map((l) => JSON.parse(l)).filter((l) => l.kind === "passed");
	assert.deepEqual(passed.map((l) => [l.texted, l.made]), [[true, true], [false, false], [false, true]]);

	// an operator's address: no text, the number not recorded
	const raw = readUsers(r.dir);
	raw["dana@example.com"] = { hash: hashPassword("cretzuel"), created: "2026-09-30T18:00:00+00:00" };
	writeFileSync(join(r.dir, "users.json"), JSON.stringify({ users: raw }));
	r.later(61);
	({ out } = await throughEmail(r, "dana@example.com", "+49 151 23456789", "198.51.100.4"));
	assert.deepEqual(out, { done: true, user: "dana@example.com" });
	assert.equal(smsCount(r), 1);
	assert.ok(!r.users.knownPhone("+4915123456789") && r.users.role("dana@example.com") === "admin");

	// a known address with a new number: the text again
	r.later(61);
	const { id, out: o } = await throughEmail(r, "ana@example.com", "+40712345679");
	assert.equal((o as View).step, "phone_code");
	assert.deepEqual(r.s.verifyPhone(id, r.lastCode("sms"), IP), { done: true, user: "ana@example.com" });
	assert.deepEqual(readUsers(r.dir)["ana@example.com"].phones, [PHONE, "+40712345679"]);
});

test("no step before its turn: the server's flow says what is proved, never the request", async () => {
	const r = rig();
	await rejects(() => r.s.verifyPhone("no-such-flow", "123456", IP), "SIGNUP_EXPIRED");
	const { id } = await r.s.start(null, { email: "ana@example.com", phone: PHONE }, IP);
	await rejects(r.s.phone(id, "+40712345679", IP, SITE), "OUT_OF_ORDER", "a number before the email's code");
	await rejects(() => r.s.verifyPhone(id, "123456", IP), "OUT_OF_ORDER", "a text's code before the email's");
	await rejects(() => r.s.changePhone(id), "OUT_OF_ORDER");
	await r.s.verifyEmail(id, r.lastCode("email"), IP, SITE);
	assert.equal(r.s.changePhone(id).step, "phone");
	await rejects(() => r.s.verifyPhone(id, r.lastCode("sms"), IP), "OUT_OF_ORDER", "the code of a number given up");
	await rejects(r.s.resend(id, IP, SITE), "OUT_OF_ORDER", "a resend with no number");
	assert.equal(r.users.count(), 0, "nobody passed");
});

test("codes: 5 wrong tries burn it, only the newest counts, it expires, it is used once", async () => {
	const r = rig();
	const { id } = await r.s.start(null, { email: "ana@example.com", phone: PHONE }, IP);
	const right = r.lastCode("email");
	const wrong = right === "000000" ? "111111" : "000000";
	const verify = (code: string) => () => r.s.verifyEmail(id, code, IP, SITE);
	await rejects(verify("12345"), "BAD_CODE", "not 6 digits: not a try");
	for (let i = 4; i >= 1; i--) {
		const e = await rejects(verify(wrong), "WRONG_CODE");
		assert.equal(e.extra.tries_left, i);
	}
	await rejects(verify(wrong), "TOO_MANY_TRIES", "the 5th");
	await rejects(verify(right), "TOO_MANY_TRIES", "burned: even the right code");
	r.later(61);
	await r.s.resend(id, IP, SITE);
	const fresh = r.lastCode("email");
	if (fresh !== right) await rejects(verify(right), "WRONG_CODE", "the older code no longer counts");
	r.later(601);
	await rejects(verify(fresh), "CODE_EXPIRED", "10 minutes later");
	r.later(61);
	await r.s.resend(id, IP, SITE);
	const c = r.lastCode("email");
	assert.equal(((await verify(` ${c.slice(0, 3)} ${c.slice(3)} `)()) as View).step, "phone_code", "spaces in a pasted code are fine");
});

test("the floor and the per-step count: 60 s between codes, 3 codes a step, changing the number buys nothing", async () => {
	const r = rig();
	const { id } = await r.s.start(null, { email: "ana@example.com", phone: PHONE }, IP);
	let e = await rejects(r.s.resend(id, IP, SITE), "WAIT");
	assert.equal(e.extra.retry_in, 60);
	r.later(30);
	e = await rejects(r.s.resend(id, IP, SITE), "WAIT");
	assert.equal(e.extra.retry_in, 30);
	assert.equal(r.s.view(r.s.flow(id)).resend_in, 30);
	r.later(31);
	await r.s.resend(id, IP, SITE);
	r.later(61);
	await r.s.resend(id, IP, SITE);
	assert.equal(r.s.view(r.s.flow(id)).sends_left, 0);
	r.later(61);
	await rejects(r.s.resend(id, IP, SITE), "LIMIT", "a 4th email code in one flow");

	await r.s.verifyEmail(id, r.lastCode("email"), IP, SITE); // the first text
	assert.equal(r.s.changePhone(id).step, "phone");
	e = await rejects(r.s.phone(id, "+40712345679", IP, SITE), "WAIT", "another number within 60 s");
	assert.equal((e.extra.view as View).step, "phone", "the page is told where the flow stands");
	r.later(61);
	await r.s.phone(id, "+40712345679", IP, SITE);
	r.later(61);
	assert.equal(r.s.changePhone(id).step, "phone");
	await r.s.phone(id, "+40712345670", IP, SITE);
	r.later(61);
	await rejects(r.s.phone(id, "+40712345671", IP, SITE), "LIMIT", "a 4th text in one flow, whatever the number");
	assert.equal(smsCount(r), 3);
});

test("limits across flows: per address, per IP, the server's daily budget; a restart keeps them", async () => {
	const r = rig({ policy: { emails_per_day: 8 } });
	const form = (email: string) => ({ email, phone: PHONE });
	for (let i = 0; i < 5; i++) {
		await r.s.start(null, form("ana@example.com"), `198.51.100.${i}`);
		r.later(61);
	}
	let e = await rejects(r.s.start(null, form("ana@example.com"), "198.51.100.99"), "LIMIT", "a 6th code email to one address in a day");
	assert.match(e.message, /this address/);
	assert.ok(Number(e.extra.retry_in) > 23 * 3600, "back in about a day");

	// on a restart the counts are rebuilt from the ledger
	const again = rig({ dir: r.dir, t: r.clock, policy: { emails_per_day: 8 } });
	await rejects(again.s.start(null, form("ana@example.com"), "198.51.100.98"), "LIMIT", "after a restart too");
	await again.s.start(null, form("bob@example.com"), "198.51.100.1");
	await again.s.start(null, form("cid@example.com"), "198.51.100.1");
	await again.s.start(null, form("dan@example.com"), "198.51.100.1");
	e = await rejects(again.s.start(null, form("eve@example.com"), "198.51.100.1"), "PAUSED", "the server's 8 emails a day");
	assert.match(e.message, /any more codes today/);
	again.later(24 * 3600 + 1);
	await again.s.start(null, form("eve@example.com"), "198.51.100.1");

	// per IP: 10 new flows an hour, 10 emails a day
	const p = rig();
	for (let i = 0; i < 10; i++) await p.s.start(null, form(`u${i}@example.com`), IP);
	e = await rejects(p.s.start(null, form("u10@example.com"), IP), "LIMIT", "an 11th flow from one address within the hour");
	assert.match(e.message, /network in the last hour/);
	p.later(3601);
	e = await rejects(p.s.start(null, form("u11@example.com"), IP), "LIMIT", "an 11th email from one address within the day");
	assert.match(e.message, /from your network today/);
	await p.s.start(null, form("u12@example.com"), "2001:db8:1:2::99");
});

test("texts: the number read at the form, then per number, per IP and the server's budget once the address is proved", async () => {
	const r = rig({ policy: { sms_per_day: 5 } });
	for (const [bad, code] of [["+1 876 555 1234", "PHONE_COUNTRY"], ["+11720555012", "BAD_PHONE"], ["0712 345 678", "BAD_PHONE"], ["+40 712 345 678; DROP", "BAD_PHONE"], ["", "BAD_PHONE"]]) {
		await rejects(r.s.start(null, { email: "p@example.com", phone: bad }, IP), code, `${bad}: refused at the form`);
	}
	assert.equal(r.dry!.sent.length, 0, "nothing sent for a number refused at the form");
	const e0 = await rejects(r.s.start(null, { email: "p@example.com", phone: "+1 876 555 1234" }, IP), "PHONE_COUNTRY");
	assert.equal(e0.message, "We can't send text messages to numbers in this country (JM).", "Jamaica is +1 but not the US");
	for (let i = 0; i < 3; i++) await throughEmail(r, `p${i}@example.com`, PHONE, `198.51.100.${i}`);
	let e = await rejects(throughEmail(r, "p3@example.com", PHONE, "198.51.100.3"), "LIMIT", "a 4th text to one number in a day, across flows");
	assert.match(e.message, /this number/);
	assert.equal((e.extra.view as View).step, "phone", "the address stays proved: another number may follow");
	await throughEmail(r, "p4@example.com", "+1 202 555 0123", "198.51.100.4");
	await throughEmail(r, "p5@example.com", "+49 151 23456789", "198.51.100.5");
	e = await rejects(throughEmail(r, "p6@example.com", "+33 6 12 34 56 78", "198.51.100.6"), "PAUSED", "the server's 5 texts a day: the 6th refused");
	assert.match(e.message, /any more text messages today/);

	// per IP: 5 texts a day
	const q = rig();
	for (let i = 0; i < 5; i++) await throughEmail(q, `q${i}@example.com`, `+4071234560${i}`);
	e = await rejects(throughEmail(q, "q9@example.com", "+40712345609"), "LIMIT");
	assert.match(e.message, /from your network today/);
});

test("blocked: an address gets no code (the same answer), a person's numbers are refused once an address is proved", async () => {
	const r = rig();
	writeFileSync(join(r.dir, "users.json"), JSON.stringify({ users: { "ana@example.com": { role: "blocked", phone: PHONE, phones: [PHONE, "+40712345677"] } } }));
	const fresh = await r.s.start(null, { email: "new@example.com", phone: "+40712345600" }, IP);
	const before = r.dry!.sent.length;
	const blocked = await r.s.start(null, { email: "ANA@example.com", phone: "+40712345600" }, "198.51.100.7");
	const strip = (v: View) => ({ ...v, email: "" });
	assert.deepEqual(strip(blocked.view), strip(fresh.view), "nothing to learn from the answer");
	assert.equal(r.dry!.sent.length, before, "nothing sent to a blocked address");
	for (const guess of ["000000", "123456", "999999", "424242"]) await rejects(() => r.s.verifyEmail(blocked.id, guess, IP, SITE), "WRONG_CODE");

	r.later(61);
	const { id } = await r.s.start(null, { email: "eve@example.com", phone: PHONE }, "198.51.100.8");
	const e = await rejects(r.s.verifyEmail(id, r.lastCode("email"), "198.51.100.8", SITE), "PHONE_BLOCKED", "the blocked person's number, with another address");
	assert.equal(e.status, 403);
	assert.equal((e.extra.view as View).step, "phone");
	await rejects(r.s.phone(id, "+40712345677", "198.51.100.8", SITE), "PHONE_BLOCKED", "their other number too");
	assert.equal(smsCount(r), 0, "no text to a blocked number");
	assert.equal(((await r.s.phone(id, "+40712345601", "198.51.100.8", SITE)) as View).step, "phone_code", "another number goes on");
});

test("one address in two flows at once: both pass, the record keeps both numbers; the last code is good once", async () => {
	const r = rig();
	const a = (await throughEmail(r, "ana@example.com", PHONE, "198.51.100.1")).id;
	r.later(61);
	const b = (await throughEmail(r, "ana@example.com", "+40712345679", "198.51.100.2")).id;
	const codeB = r.lastCode("sms");
	const codeA = /\b(\d{6})\b/.exec(r.dry!.sent.filter((m) => m.kind === "sms")[0].text)![1];
	assert.deepEqual(r.s.verifyPhone(a, codeA, IP), { done: true, user: "ana@example.com" });
	await rejects(() => r.s.verifyPhone(a, codeA, IP), "SIGNUP_EXPIRED", "a double submit of the last code");
	assert.deepEqual(r.s.verifyPhone(b, codeB, IP), { done: true, user: "ana@example.com" });
	assert.deepEqual(readUsers(r.dir)["ana@example.com"].phones, [PHONE, "+40712345679"]);
	assert.equal(r.users.count(), 1);
});

test("a provider failing: SEND_FAILED, still counted (no resend loop), the ledger says so; an undeliverable number goes back a step", async () => {
	let fail = true;
	let invalid = false;
	const sent: string[] = [];
	const flaky: Senders = {
		name: "flaky",
		async email(m) {
			sent.push(m.text);
			if (fail) throw new SendError("resend", "Resend answered 500: down");
			return { id: "em_1" };
		},
		async sms(to, text) {
			sent.push(text);
			if (fail) throw new SendError("sakari", "Sakari cannot be reached: ETIMEDOUT");
			return { id: "sm_1", invalid };
		},
	};
	const r = rig({ senders: flaky });
	const e = await rejects(r.s.start(null, { email: "ana@example.com", phone: PHONE }, IP), "SEND_FAILED");
	assert.match(e.message, /could not be sent/);
	const ledger = () => readFileSync(join(r.dir, "signup-log.jsonl"), "utf-8").trim().split("\n").map((l) => JSON.parse(l));
	assert.equal(ledger().at(-1).ok, false);
	assert.match(ledger().at(-1).error, /down/);
	// a failed send still counts; a retry is a new start
	fail = false;
	const ok = await r.s.start(null, { email: "ana@example.com", phone: PHONE }, IP);
	await rejects(r.s.resend(ok.id, IP, SITE), "WAIT", "the floor holds after a send");
	const code = /\b(\d{6})\b/.exec(sent.at(-1)!)![1];
	invalid = true;
	const u = await rejects(r.s.verifyEmail(ok.id, code, IP, SITE), "UNDELIVERABLE");
	assert.match(u.message, /can't receive text messages/);
	assert.equal((u.extra.view as View).step, "phone", "back to the number");
	assert.equal(r.s.view(r.s.flow(ok.id)).step, "phone");
	assert.equal(ledger().at(-1).error, "undeliverable");
	invalid = false;
	fail = true;
	r.later(61);
	const f = await rejects(r.s.phone(ok.id, "+40712345679", IP, SITE), "SEND_FAILED");
	assert.equal((f.extra.view as View).step, "phone_code", "a code may have gone: resend or change the number");
	await rejects(r.s.resend(ok.id, IP, SITE), "WAIT");
});

test("a restart in the middle: the flow, its tries and its code survive (signups.json); the old sign-up's flows do not", async () => {
	const r = rig();
	const { id } = await r.s.start(null, { email: "ana@example.com", phone: PHONE }, IP);
	const code = r.lastCode("email");
	const wrong = code === "000000" ? "111111" : "000000";
	for (let i = 0; i < 3; i++) await rejects(r.s.verifyEmail(id, wrong, IP, SITE), "WRONG_CODE");
	const after = rig({ dir: r.dir, t: r.clock });
	const e = await rejects(after.s.verifyEmail(id, wrong, IP, SITE), "WRONG_CODE");
	assert.equal(e.extra.tries_left, 1, "the tries were kept: a restart is no reset");
	assert.equal(((await after.s.verifyEmail(id, code, IP, SITE)) as View).step, "phone_code");
	// a flow expires after 30 minutes
	after.later(1801);
	assert.equal(after.s.flow(id), null);
	assert.deepEqual(after.s.view(null, true), { step: "details", expired: true, work_email: true });
	await rejects(r.s.phone(id, PHONE, IP, SITE), "SIGNUP_EXPIRED");
	const third = rig({ dir: r.dir, t: r.clock });
	assert.equal(third.s.flow(id), null, "and is not read back after a restart either");
	// expired flows are dropped from the file at the next save
	const four = rig();
	const ids: string[] = [];
	for (let i = 0; i < 4; i++) ids.push((await four.s.start(null, { email: `gone${i}@example.com`, phone: PHONE }, `198.51.100.${i}`)).id);
	four.later(1801);
	await four.s.start(null, { email: "fresh@example.com", phone: PHONE }, "198.51.100.9");
	const kept = Object.keys(JSON.parse(readFileSync(join(four.dir, "signups.json"), "utf-8")).flows);
	assert.equal(kept.length, 1, "only the fresh flow is in the file");
	assert.ok(!ids.some((x) => kept.includes(x)));
	// a flow of the sign-up with a password (before the check) is not read back
	const old = rig();
	writeFileSync(join(old.dir, "signups.json"), JSON.stringify({ flows: { abc: { id: "abc", created: old.clock.now, touched: old.clock.now, email: "x@example.com", email_ok: old.clock.now, phone_ok: old.clock.now } } }));
	assert.equal(rig({ dir: old.dir, t: old.clock }).s.flow("abc"), null);
});

test("at the same moment: two resends, two numbers — one gets through, the other waits", async () => {
	const r = rig();
	const { id } = await r.s.start(null, { email: "ana@example.com", phone: PHONE }, IP);
	r.later(61);
	const both = await Promise.allSettled([r.s.resend(id, IP, SITE), r.s.resend(id, IP, SITE)]);
	assert.deepEqual(both.map((x) => x.status).sort(), ["fulfilled", "rejected"]);
	assert.equal(((both.find((x) => x.status === "rejected") as PromiseRejectedResult).reason as SignupError).code, "WAIT");
	await r.s.verifyEmail(id, r.lastCode("email"), IP, SITE);
	r.s.changePhone(id);
	r.later(61);
	const two = await Promise.allSettled([r.s.phone(id, "+40712345670", IP, SITE), r.s.phone(id, "+40712345679", IP, SITE)]);
	assert.deepEqual(two.map((x) => x.status).sort(), ["fulfilled", "rejected"]);
	assert.equal(smsCount(r), 2);
	const won = (two.find((x) => x.status === "fulfilled") as PromiseFulfilledResult<View>).value;
	assert.deepEqual([won.step, r.s.view(r.s.flow(id)).phone], ["phone_code", won.phone], "the flow names the number the text went to");
});

test("phone numbers and addresses", () => {
	const all = SIGNUP_DEFAULTS.sms_countries;
	assert.deepEqual(parsePhone("+40 712 345 678", all), { ok: true, e164: "+40712345678", country: "RO" });
	assert.deepEqual(parsePhone("0040712345678", all), { ok: true, e164: "+40712345678", country: "RO" });
	assert.deepEqual(parsePhone("+1 (202) 555-0123", all), { ok: true, e164: "+12025550123", country: "US" });
	assert.equal((parsePhone("+1 416 555 0123", all) as any).country, "CA");
	assert.deepEqual(parsePhone("+1 876 555 1234", all), { ok: false, why: "PHONE_COUNTRY", country: "JM" });
	assert.deepEqual(parsePhone("+1 284 555 1234", all), { ok: false, why: "PHONE_COUNTRY", country: "VG" });
	for (const bad of ["", "0712345678", "+11720555012", "+999123", "+40 7", "+40712345678x", "a".repeat(50), null, 40712345678]) {
		assert.equal((parsePhone(bad, all) as any).why, "BAD_PHONE", String(bad));
	}
	assert.deepEqual(parsePhone("+40712345678", ["DE"]), { ok: false, why: "PHONE_COUNTRY", country: "RO" });
	assert.equal(formatPhone("+40712345678"), "+40 712 345 678");
	assert.equal(ipKey("203.0.113.7"), "203.0.113.7");
	assert.equal(ipKey("::ffff:203.0.113.7"), "203.0.113.7");
	assert.equal(ipKey("2001:db8:1:2:aaaa::1"), "2001:db8:1:2::/64");
	assert.equal(ipKey("2001:0db8:0001:0002:ffff:ffff:ffff:ffff"), "2001:db8:1:2::/64");
	assert.equal(ipKey("2001:db8::1"), "2001:db8:0:0::/64");
	assert.equal(ipKey(""), "unknown");
});

test("the policy: defaults, resumes.toml over them, nonsense ignored", () => {
	const p = policyOf({ signup: { sms_per_day: 10, sms_countries: ["ro", " de "], bogus: 1, code_ttl: -5 }, limits: { usd_per_day: 2.5, turns_per_day: "lots" } });
	assert.equal(p.signup.sms_per_day, 10);
	assert.deepEqual(p.signup.sms_countries, ["RO", "DE"]);
	assert.equal(p.signup.code_ttl, 600, "a negative number is ignored");
	assert.ok(!("bogus" in p.signup));
	assert.equal(p.limits.usd_per_day, 2.5);
	assert.equal(p.limits.turns_per_day, 100, "a string is ignored");
	assert.deepEqual(policyOf(null), policyOf({}));
	assert.equal(policyOf({}).signup.work_email, true, "work addresses only, by default");
	assert.equal(policyOf({ signup: { work_email: false } }).signup.work_email, false);
	assert.equal(policyOf({ signup: { work_email: 0 } }).signup.work_email, true, "a number is not a yes/no");
});

test("the domain lists: throwaway and free providers known, companies (and the tests' example.com) on neither", () => {
	const { disposable, free } = mailLists();
	assert.ok(disposable.size > 5000, `${disposable.size} throwaway domains`);
	for (const d of ["mailinator.com", "yopmail.com", "guerrillamail.com", "10minutemail.com", "temp-mail.org"]) assert.ok(disposable.has(d), d);
	for (const d of ["gmail.com", "googlemail.com", "outlook.com", "hotmail.co.uk", "yahoo.ro", "icloud.com", "proton.me", "gmx.de", "duck.com"]) assert.ok(free.has(d), d);
	for (const d of ["google.com", "microsoft.com", "apple.com", "facebook.com", "zoho.com", "stripe.com", "gentlebits.net", "example.com", "nus.edu.sg"]) {
		assert.ok(!listed(disposable, d) && !listed(free, d), `${d} is listed`);
	}
	assert.equal(listed(disposable, "abc.mailinator.com"), "mailinator.com", "a subdomain counts as its domain");
	assert.equal(listed(free, "ana.anonaddy.com"), "anonaddy.com");
	assert.equal(listed(free, "gmail.com.example.org"), null, "only a suffix counts");
	assert.equal([...free].filter((d) => disposable.has(d)).length <= 5, true, "the two lists mostly apart");
});

test("an address's domain: throwaway never, personal while work_email, a domain that takes no mail, a throwaway mail server", async () => {
	const looked: string[] = [];
	const dns: Record<string, MxAnswer> = {
		"acme.test": { hosts: ["aspmx.l.google.com"] },
		"nomail.test": "none",
		"sneaky.test": { hosts: ["mail.guerrillamail.com"] },
		"slowdns.test": "unknown",
	};
	const mx = async (d: string) => (looked.push(d), dns[d] ?? "unknown");
	const m = new MailDomains(mx);
	assert.deepEqual(await m.check("ana@mailinator.com", false), { ok: false, why: "DISPOSABLE_EMAIL", domain: "mailinator.com" });
	assert.deepEqual(await m.check("ana@gmail.com", true), { ok: false, why: "PERSONAL_EMAIL", domain: "gmail.com" });
	assert.deepEqual(await m.check("ana@acme.test", true), { ok: true });
	assert.deepEqual(await m.check("ana@nomail.test", true), { ok: false, why: "NO_MAIL", domain: "nomail.test" });
	assert.deepEqual(await m.check("ana@sneaky.test", true), { ok: false, why: "DISPOSABLE_EMAIL", domain: "sneaky.test" });
	assert.deepEqual(await m.check("ana@slowdns.test", true), { ok: true }, "DNS silent: let through (the code is the test)");
	assert.deepEqual(looked, ["acme.test", "nomail.test", "sneaky.test", "slowdns.test"], "the lists decide before DNS is asked");
	assert.deepEqual(await m.check("bob@gmail.com", false), { ok: true }, "work_email off: a personal mailbox will do");
	await m.check("bob@acme.test", true);
	await m.check("eve@slowdns.test", true);
	assert.deepEqual(looked.slice(4), ["gmail.com", "slowdns.test"], "an answer is kept for an hour; a silence is asked again");
	assert.deepEqual(await new MailDomains(null).check("ana@nomail.test", true), { ok: true }, "no DNS (the dry run): the lists only");
});

test("the form refuses those addresses before anything is counted or sent", async () => {
	const r = rig({ mx: async (d) => (d === "nomail.test" ? "none" : { hosts: [`mx.${d}`] }) });
	const form = (email: string) => ({ email, phone: PHONE });
	const e1 = await rejects(r.s.start(null, form("Ana@GMail.com"), IP), "PERSONAL_EMAIL");
	assert.equal(e1.message, "Please use your business email address, as personal addresses (gmail.com) are not accepted.");
	assert.equal(e1.status, 422);
	const e2 = await rejects(r.s.start(null, form("x@yopmail.com"), IP), "DISPOSABLE_EMAIL");
	assert.equal(e2.message, "Please use your business email address, as temporary addresses are not accepted.");
	const e3 = await rejects(r.s.start(null, form("x@nomail.test"), IP), "NO_MAIL");
	assert.equal(e3.message, "Please check the address, as nomail.test does not appear to receive email.");
	assert.equal(r.dry!.sent.length, 0, "nothing sent");
	assert.ok(!existsSync(join(r.dir, "signup-log.jsonl")), "nothing in the ledger: a refusal uses up no limit");
	for (let i = 0; i < 15; i++) await rejects(r.s.start(null, form("x@gmail.com"), IP), "PERSONAL_EMAIL", "more refusals than the IP's flows an hour");
	const { view } = await r.s.start(null, form("ana@acme.test"), IP);
	assert.equal(view.step, "email_code", "a work address goes on");

	const home = rig({ policy: { work_email: false } });
	assert.equal((await home.s.start(null, form("ana@gmail.com"), IP)).view.step, "email_code", "work_email off: gmail.com will do");
	await rejects(home.s.start(null, form("ana@mailinator.com"), IP), "DISPOSABLE_EMAIL", "a throwaway one never");
	assert.equal(home.s.view(null).work_email, false, "the page is told, for its label");
});

/** A stub of the two providers' HTTP: the routes answer in turn, every call is kept. */
function stubHttp(answers: Record<string, ({ status: number; text: string } | (() => { status: number; text: string }))[]>) {
	const calls: { url: string; init: { method: string; headers: Record<string, string>; body?: string } }[] = [];
	const http: Http = async (url, init) => {
		calls.push({ url, init });
		const key = Object.keys(answers).find((k) => url.endsWith(k));
		const a = key ? answers[key].shift() : undefined;
		if (!a) throw new Error(`no stub for ${url}`);
		return typeof a === "function" ? a() : a;
	};
	return { http, calls };
}

test("Sakari: the token kept and shared, a 401 retried once, undeliverable only on its word, a 2xx is sent", async () => {
	const tok = (t: string) => ({ status: 200, text: JSON.stringify({ access_token: t, expires_in: 3600, token_type: "Bearer" }) });
	const msg = (body: unknown, status = 201) => ({ status, text: JSON.stringify(body) });
	const { http, calls } = stubHttp({
		"/oauth2/token": [tok("t1"), tok("t2")],
		"/v1/accounts/acc%201/messages": [
			msg({ success: true, data: { messages: [{ id: "m1", status: "queued" }], valid: 1, invalid: [] } }),
			msg({ data: { valid: 0, invalid: [{ mobile: "+40712345678", reason: "unroutable" }] } }),
			{ status: 200, text: "<html>accepted</html>" },
			msg({ error: "expired token" }, 401),
			msg({ data: { messages: [{ id: "m4" }] } }),
			msg({ error: "account suspended" }, 402),
		],
	});
	let now = 1_000_000;
	const s = new Sakari("acc 1", "cid", "sec ret", http, "https://api.sakari.test", () => now);
	const [a, b] = await Promise.all([s.send("+40712345678", "123456 is your code"), s.send("+40712345678", "x")]);
	assert.deepEqual(a, { id: "m1", invalid: false });
	assert.deepEqual(b, { id: null, invalid: true }, "valid: 0 with an invalid list: undeliverable");
	assert.equal(calls.filter((c) => c.url.endsWith("/oauth2/token")).length, 1, "one token for two sends at once");
	const tokenCall = calls[0];
	assert.equal(tokenCall.init.headers["Content-Type"], "application/x-www-form-urlencoded");
	assert.equal(tokenCall.init.body, "grant_type=client_credentials&client_id=cid&client_secret=sec+ret");
	const send = calls[1];
	assert.equal(send.init.headers.Authorization, "Bearer t1");
	assert.deepEqual(JSON.parse(send.init.body!), { contacts: [{ mobile: "+40712345678" }], template: "123456 is your code" });
	assert.deepEqual(await s.send("+40712345678", "x"), { id: null, invalid: false }, "an unparsable 2xx is a message sent");
	now += 10_000;
	assert.deepEqual(await s.send("+40712345678", "x"), { id: "m4", invalid: false }, "a 401: a fresh token, one more try");
	assert.equal(calls.filter((c) => c.url.endsWith("/oauth2/token")).length, 2);
	await assert.rejects(s.send("+40712345678", "x"), (e: any) => e instanceof SendError && /402: account suspended/.test(e.message));
	const bad = new Sakari("a", "c", "s", stubHttp({ "/oauth2/token": [{ status: 401, text: '{"error":"invalid_client"}' }] }).http);
	await assert.rejects(bad.send("+40712345678", "x"), (e: any) => e instanceof SendError && /refused the token \(401\): invalid_client/.test(e.message));
	const down = new Sakari("a", "c", "s", async () => {
		throw new Error("ECONNREFUSED");
	});
	await assert.rejects(down.send("+40712345678", "x"), (e: any) => e instanceof SendError && /cannot be reached: ECONNREFUSED/.test(e.message));
});

test("Resend: one POST with an idempotency key; anything but a 2xx is SendError", async () => {
	const { http, calls } = stubHttp({ "/emails": [{ status: 200, text: '{"id":"em_123"}' }, { status: 422, text: '{"name":"validation_error","message":"The from address is not verified"}' }] });
	const r = new Resend("re_key", "Semantic search <noreply@signup.example>", http, "https://api.resend.test");
	assert.deepEqual(await r.send({ to: "ana@example.com", subject: "s", text: "t", html: "<p>h</p>", key: "flow-email-1" }), { id: "em_123" });
	const c = calls[0];
	assert.equal(c.url, "https://api.resend.test/emails");
	assert.deepEqual(c.init.headers, { Authorization: "Bearer re_key", "Content-Type": "application/json", "Idempotency-Key": "flow-email-1" });
	assert.deepEqual(JSON.parse(c.init.body!), { from: "Semantic search <noreply@signup.example>", to: ["ana@example.com"], subject: "s", text: "t", html: "<p>h</p>" });
	await assert.rejects(r.send({ to: "a@b.c", subject: "s", text: "t", html: "h", key: "k" }), (e: any) => e instanceof SendError && /422: The from address is not verified/.test(e.message));
});

test("the senders from the environment: all five keys or none, the dry run on request", () => {
	assert.deepEqual(sendersFrom({}).missing, ["RESEND_API_KEY", "RESEND_FROM", "SAKARI_ACCOUNT_ID", "SAKARI_CLIENT_ID", "SAKARI_CLIENT_SECRET"]);
	const some = sendersFrom({ RESEND_API_KEY: "k", RESEND_FROM: "f", SAKARI_ACCOUNT_ID: "a", SAKARI_CLIENT_ID: " " });
	assert.equal(some.senders, null);
	assert.deepEqual(some.missing, ["SAKARI_CLIENT_ID", "SAKARI_CLIENT_SECRET"]);
	const all = sendersFrom({ RESEND_API_KEY: "k", RESEND_FROM: "f", SAKARI_ACCOUNT_ID: "a", SAKARI_CLIENT_ID: "c", SAKARI_CLIENT_SECRET: "s" });
	assert.equal(all.senders?.name, "resend + sakari");
	assert.equal(sendersFrom({ RESUMES_SIGNUP_DRYRUN: "1" }).senders?.name, "dry run");
});
