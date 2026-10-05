/**
 * The sign-up: verify the email, verify the phone, set a password → a member account. The flow lives on the server and
 * the browser holds only its id, so no step can be skipped and no counter reset from the browser. Codes are kept only
 * as HMACs; every send goes to `.resumes/signup-log.jsonl`, which the limits count, so a restart resets no limit.
 */
import { createHmac, randomBytes, randomInt, timingSafeEqual } from "node:crypto";
import { appendFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { type Users, hashPassword } from "./auth.js";
import { ResumesError } from "./errors.js";
import { writeJsonAtomic, writeTextAtomic } from "./files.js";
import { MailDomains } from "./mail-domains.js";
import { formatPhone, parsePhone } from "./phone.js";
import type { SignupPolicy } from "./policy.js";
import { SendError, type Senders } from "./senders.js";

export const FLOWS_FILE = "signups.json";
export const LEDGER_FILE = "signup-log.jsonl";
export const PRODUCT = "Semantic search";
const HOUR = 3600_000;
const DAY = 24 * HOUR;
const KEEP_DAYS = 30;

/** A refusal the page shows as is: a code for the page, an HTTP status, one line of text, and what else it needs. */
export class SignupError extends Error {
	constructor(public code: string, public status: number, message: string, public extra: Record<string, unknown> = {}) {
		super(message);
	}
}

export type Step = "email" | "email_code" | "phone" | "phone_code" | "password";

export interface Flow {
	id: string;
	created: number;
	touched: number;
	ip: string;
	email: string;
	email_known: boolean; // the address had an account when the flow began: it got "you already have an account", no code
	email_code: string | null;
	email_exp: number;
	email_tries: number;
	email_sends: number[];
	email_ok: number | null;
	phone: string | null;
	phone_code: string | null;
	phone_exp: number;
	phone_tries: number;
	phone_sends: number[]; // every SMS of this flow, whichever number
	phone_ok: number | null;
}

export interface View {
	step: Step;
	email?: string;
	phone?: string;
	resend_in?: number; // seconds before another code of this step may be asked for
	sends_left?: number;
	expires_in?: number; // seconds this flow has left
	expired?: boolean;
	work_email?: boolean; // the first step: only a work address will do ([signup] work_email)
}

interface Entry {
	at: number;
	kind: "flow" | "email" | "sms" | "account";
	to?: string;
	ip: string;
	ok?: boolean;
	id?: string | null;
	error?: string;
	flow?: string;
}

/** Where the request came from, for the messages: the public name the page was reached by. */
export interface Site {
	host: string;
	https: boolean;
}

export const normalizeEmail = (v: unknown): string => String(v ?? "").trim().toLowerCase();
const EMAIL = /^[^\s@"<>(),;:\\[\]]{1,64}@[^\s@"<>(),;:\\[\]]+\.[^\s@"<>(),;:\\[\]]{2,}$/;

/** An address to count by: IPv4 as it is, IPv6 by its /64 (one household or one server gets a whole /64). */
export function ipKey(ip: string): string {
	let a = String(ip || "").trim().toLowerCase();
	if (a.startsWith("::ffff:") && a.includes(".")) a = a.slice(7);
	if (!a.includes(":")) return a || "unknown";
	a = a.replace(/%.*$/, "");
	const [head, tail] = a.split("::");
	const h = head ? head.split(":") : [];
	const t = tail !== undefined ? (tail ? tail.split(":") : []) : [];
	const groups = tail !== undefined ? [...h, ...Array(Math.max(0, 8 - h.length - t.length)).fill("0"), ...t] : h;
	return `${groups.slice(0, 4).map((g) => (g || "0").replace(/^0+(?=.)/, "")).join(":")}::/64`;
}

export class Signup {
	private flows = new Map<string, Flow>();
	private entries: Entry[] = []; // the ledger's last 24 h
	readonly flowsPath: string;
	readonly ledgerPath: string;

	constructor(
		public stateDir: string,
		public users: Users,
		public senders: Senders,
		public policy: () => SignupPolicy,
		public secret: () => Buffer,
		public now: () => number = () => Date.now(),
		public mail: MailDomains = new MailDomains(null),
	) {
		this.flowsPath = join(stateDir, FLOWS_FILE);
		this.ledgerPath = join(stateDir, LEDGER_FILE);
		this.loadFlows();
		this.loadLedger();
	}

	private loadFlows(): void {
		let raw: any = null;
		try {
			raw = JSON.parse(readFileSync(this.flowsPath, "utf-8"));
		} catch {
			raw = null;
		}
		const ttl = this.policy().flow_ttl * 1000;
		let dropped = false;
		for (const [id, f] of Object.entries((raw && typeof raw === "object" ? raw.flows : null) || {})) {
			const flow = f as Flow;
			if (!flow || typeof flow !== "object" || flow.id !== id || typeof flow.email !== "string") continue;
			if (this.now() - flow.touched > ttl) {
				dropped = true;
				continue;
			}
			this.flows.set(id, flow);
		}
		if (dropped) this.saveFlows();
	}

	/** Also drops the flows nobody came back to (at most a day's emails of them: every flow begins with a capped email). */
	private saveFlows(): void {
		const ttl = this.policy().flow_ttl * 1000;
		for (const [id, f] of this.flows) if (this.now() - f.touched > ttl) this.flows.delete(id);
		writeJsonAtomic(this.flowsPath, { flows: Object.fromEntries(this.flows) }, 0o600, false);
	}

	private loadLedger(): void {
		let lines: string[] = [];
		try {
			lines = readFileSync(this.ledgerPath, "utf-8").split("\n").filter(Boolean);
		} catch {
			return;
		}
		const now = this.now();
		const kept: string[] = [];
		for (const line of lines) {
			let e: Entry | null = null;
			try {
				e = JSON.parse(line);
			} catch {
				e = null;
			}
			if (!e || typeof e.at !== "number") continue;
			if (now - e.at <= KEEP_DAYS * DAY) kept.push(line);
			if (now - e.at <= DAY) this.entries.push(e);
		}
		if (kept.length !== lines.length) writeTextAtomic(this.ledgerPath, kept.length ? `${kept.join("\n")}\n` : "");
	}

	/** Counted from now on; written to the file when its outcome is known (`write`). */
	private note(e: Entry): Entry {
		this.entries.push(e);
		return e;
	}

	private write(e: Entry): void {
		try {
			appendFileSync(this.ledgerPath, `${JSON.stringify(e)}\n`, { mode: 0o600 });
		} catch (err) {
			console.error(`signup · the ledger could not be written: ${(err as Error).message}`);
		}
	}

	private count(kind: Entry["kind"], windowMs: number, match: (e: Entry) => boolean = () => true): number {
		const since = this.now() - windowMs;
		if (this.entries.length && this.entries[0].at < this.now() - DAY) this.entries = this.entries.filter((e) => e.at >= this.now() - DAY);
		let n = 0;
		for (const e of this.entries) if (e.kind === kind && e.at >= since && match(e)) n++;
		return n;
	}

	/** Seconds until the oldest of the counted entries leaves the window (when a limit opens again). */
	private reopensIn(kind: Entry["kind"], windowMs: number, match: (e: Entry) => boolean = () => true): number {
		const since = this.now() - windowMs;
		const oldest = this.entries.find((e) => e.kind === kind && e.at >= since && match(e));
		return oldest ? Math.max(1, Math.ceil((oldest.at + windowMs - this.now()) / 1000)) : 0;
	}

	flow(id: string | undefined | null): Flow | null {
		if (!id) return null;
		const f = this.flows.get(id);
		if (!f) return null;
		if (this.now() - f.touched > this.policy().flow_ttl * 1000) {
			this.flows.delete(id);
			this.saveFlows();
			return null;
		}
		return f;
	}

	step(f: Flow): Step {
		if (!f.email_ok) return "email_code";
		if (!f.phone_ok) return f.phone ? "phone_code" : "phone";
		return "password";
	}

	view(f: Flow | null, hadOne = false): View {
		const p = this.policy();
		if (!f) return { step: "email", ...(hadOne ? { expired: true } : {}), work_email: p.work_email };
		const step = this.step(f);
		const v: View = { step, email: f.email, expires_in: Math.max(0, Math.ceil((f.touched + p.flow_ttl * 1000 - this.now()) / 1000)) };
		if (f.phone) v.phone = formatPhone(f.phone);
		const sends = step === "email_code" ? f.email_sends : step === "phone_code" || step === "phone" ? f.phone_sends : null;
		if (sends) {
			const last = sends.at(-1);
			v.resend_in = last ? Math.max(0, Math.ceil((last + p.resend_after * 1000 - this.now()) / 1000)) : 0;
			v.sends_left = Math.max(0, p.codes_per_step - sends.length);
		}
		return v;
	}

	private touch(f: Flow): void {
		f.touched = this.now();
		this.flows.set(f.id, f);
		this.saveFlows();
	}

	private hmac(f: Flow, step: "email" | "phone", code: string): string {
		return createHmac("sha256", this.secret()).update(`${f.id}:${step}:${code}`).digest("base64url");
	}

	private emailLimits(to: string, ip: string): void {
		const p = this.policy();
		if (this.count("email", DAY) >= p.emails_per_day) {
			console.warn(`signup · paused: ${p.emails_per_day} emails in 24 h (the server's budget, [signup] emails_per_day)`);
			throw new SignupError("PAUSED", 503, "Sign-up is paused for today — try again tomorrow.", { retry_in: this.reopensIn("email", DAY) });
		}
		if (this.count("email", DAY, (e) => e.to === to) >= p.per_email_day) {
			throw new SignupError("LIMIT", 429, "Too many codes for this address today — try again tomorrow.", { retry_in: this.reopensIn("email", DAY, (e) => e.to === to) });
		}
		if (this.count("email", DAY, (e) => e.ip === ip) >= p.per_ip_emails_day) {
			throw new SignupError("LIMIT", 429, "Too many codes from your network today — try again tomorrow.", { retry_in: this.reopensIn("email", DAY, (e) => e.ip === ip) });
		}
	}

	private smsLimits(to: string, ip: string): void {
		const p = this.policy();
		if (this.count("sms", DAY) >= p.sms_per_day) {
			console.warn(`signup · paused: ${p.sms_per_day} SMS in 24 h (the server's budget, [signup] sms_per_day)`);
			throw new SignupError("PAUSED", 503, "Sign-up is paused for today — try again tomorrow.", { retry_in: this.reopensIn("sms", DAY) });
		}
		if (this.count("sms", DAY, (e) => e.to === to) >= p.per_phone_day) {
			throw new SignupError("LIMIT", 429, "Too many codes to this number today — try again tomorrow, or use another number.", { retry_in: this.reopensIn("sms", DAY, (e) => e.to === to) });
		}
		if (this.count("sms", DAY, (e) => e.ip === ip) >= p.per_ip_sms_day) {
			throw new SignupError("LIMIT", 429, "Too many text messages from your network today — try again tomorrow.", { retry_in: this.reopensIn("sms", DAY, (e) => e.ip === ip) });
		}
	}

	private stepLimits(sends: number[], what: "email" | "phone"): void {
		const p = this.policy();
		if (sends.length >= p.codes_per_step) {
			throw new SignupError("LIMIT", 429, `That was the last code for this sign-up${what === "phone" ? "'s phone step" : ""} — start again.`);
		}
		const last = sends.at(-1);
		if (last && this.now() - last < p.resend_after * 1000) {
			const wait = Math.ceil((last + p.resend_after * 1000 - this.now()) / 1000);
			throw new SignupError("WAIT", 429, `Wait ${wait} s before asking for another code.`, { retry_in: wait });
		}
	}

	private async sendEmail(f: Flow, ip: string, site: Site): Promise<void> {
		const p = this.policy();
		const blocked = f.email_known && this.users.taken(f.email) && !this.users.has(f.email);
		let code = "";
		f.email_tries = 0;
		if (f.email_known) f.email_code = null;
		else {
			code = String(randomInt(0, 1_000_000)).padStart(6, "0");
			f.email_code = this.hmac(f, "email", code);
		}
		f.email_exp = this.now() + p.code_ttl * 1000;
		f.email_sends.push(this.now());
		this.touch(f); // saved before the provider is called: a failed send still counts, and a retry sees the floor
		const e = this.note({ at: this.now(), kind: "email", to: f.email, ip, flow: f.id.slice(0, 8) });
		if (blocked) {
			// a blocked account gets no mail at all; the answer is the same as for anyone (nothing to learn from it)
			Object.assign(e, { ok: true, id: null, error: "blocked: not sent" });
			this.write(e);
			return;
		}
		const m = f.email_known ? knownEmail(site) : codeEmail(code, p.code_ttl);
		try {
			const r = await this.senders.email({ to: f.email, ...m, key: `${f.id}-email-${f.email_sends.length}` });
			Object.assign(e, { ok: true, id: r.id });
			this.write(e);
		} catch (err) {
			Object.assign(e, { ok: false, error: String((err as Error)?.message || err).slice(0, 300) });
			this.write(e);
			console.error(`signup · the email to ${f.email} was not sent: ${e.error}`);
			if (err instanceof SendError) throw new SignupError("SEND_FAILED", 502, "The email could not be sent just now — try “Resend code” in a minute.");
			throw err;
		}
	}

	private async sendSms(f: Flow, phone: string, ip: string, site: Site): Promise<void> {
		const p = this.policy();
		const code = String(randomInt(0, 1_000_000)).padStart(6, "0");
		f.phone = phone;
		f.phone_ok = null;
		f.phone_code = this.hmac(f, "phone", code);
		f.phone_exp = this.now() + p.code_ttl * 1000;
		f.phone_tries = 0;
		f.phone_sends.push(this.now());
		this.touch(f);
		const e = this.note({ at: this.now(), kind: "sms", to: phone, ip, flow: f.id.slice(0, 8) });
		let r: { id: string | null; invalid: boolean };
		try {
			r = await this.senders.sms(phone, `${code} is your ${PRODUCT} code.\n\n@${site.host.replace(/:\d+$/, "")} #${code}`);
		} catch (err) {
			Object.assign(e, { ok: false, error: String((err as Error)?.message || err).slice(0, 300) });
			this.write(e);
			console.error(`signup · the SMS to ${phone} was not sent: ${e.error}`);
			if (err instanceof SendError) throw new SignupError("SEND_FAILED", 502, "The text message could not be sent just now — try “Resend code” in a minute.");
			throw err;
		}
		Object.assign(e, { ok: !r.invalid, id: r.id, ...(r.invalid ? { error: "undeliverable" } : {}) });
		this.write(e);
		if (r.invalid) {
			const g = this.flows.get(f.id) || f; // the flow as it is now (an await happened)
			if (g.phone === phone) {
				g.phone = null;
				g.phone_code = null;
				this.touch(g);
			}
			throw new SignupError("UNDELIVERABLE", 422, "This number can't receive text messages — use another one.");
		}
	}

	/** Step 1: an address → a new flow and its code sent. The answer is the same whether the address has an account or
	 * not: the email says which. */
	async start(oldId: string | null | undefined, input: unknown, ip: string, site: Site): Promise<{ id: string; view: View }> {
		const p = this.policy();
		const email = normalizeEmail(input);
		if (!EMAIL.test(email) || email.length > 200) throw new SignupError("BAD_EMAIL", 422, "That doesn't look like an email address.");
		const mail = await this.mail.check(email, p.work_email);
		if (!mail.ok) {
			if (mail.why === "DISPOSABLE_EMAIL") throw new SignupError(mail.why, 422, "Please use your business email address, as temporary addresses are not accepted.");
			if (mail.why === "PERSONAL_EMAIL") throw new SignupError(mail.why, 422, `Please use your business email address, as personal addresses (${mail.domain}) are not accepted.`);
			throw new SignupError(mail.why, 422, `Please check the address, as ${mail.domain} does not appear to receive email.`);
		}
		if (this.count("flow", HOUR, (e) => e.ip === ip) >= p.per_ip_flows_hour) {
			throw new SignupError("LIMIT", 429, "Too many sign-ups from your network in the last hour — try again later.", { retry_in: this.reopensIn("flow", HOUR, (e) => e.ip === ip) });
		}
		this.emailLimits(email, ip);
		if (oldId && this.flows.has(oldId)) this.flows.delete(oldId);
		const now = this.now();
		const f: Flow = {
			id: randomBytes(32).toString("base64url"),
			created: now,
			touched: now,
			ip,
			email,
			email_known: this.users.taken(email),
			email_code: null,
			email_exp: 0,
			email_tries: 0,
			email_sends: [],
			email_ok: null,
			phone: null,
			phone_code: null,
			phone_exp: 0,
			phone_tries: 0,
			phone_sends: [],
			phone_ok: null,
		};
		this.write(this.note({ at: now, kind: "flow", ip, flow: f.id.slice(0, 8) }));
		await this.sendEmail(f, ip, site);
		return { id: f.id, view: this.view(f) };
	}

	private need(id: string | null | undefined): Flow {
		const f = this.flow(id);
		if (!f) throw new SignupError("SIGNUP_EXPIRED", 410, "This sign-up has run out — start again.");
		return f;
	}

	private check(f: Flow, step: "email" | "phone", input: unknown): void {
		const p = this.policy();
		const code = String(input ?? "").replace(/\s+/g, "");
		if (!/^\d{6}$/.test(code)) throw new SignupError("BAD_CODE", 422, "The code is the 6 digits from the message.");
		const tries = step === "email" ? f.email_tries : f.phone_tries;
		const stored = step === "email" ? f.email_code : f.phone_code;
		const exp = step === "email" ? f.email_exp : f.phone_exp;
		if (tries >= p.code_tries) throw new SignupError("TOO_MANY_TRIES", 429, "Too many wrong tries — ask for a new code.");
		if (stored && this.now() > exp) throw new SignupError("CODE_EXPIRED", 410, "That code has expired — ask for a new one.");
		const want = Buffer.from(stored || "x".repeat(43));
		const got = Buffer.from(this.hmac(f, step, code));
		const right = !!stored && want.length === got.length && timingSafeEqual(want, got);
		if (right) return;
		const n = tries + 1;
		if (step === "email") f.email_tries = n;
		else f.phone_tries = n;
		if (n >= p.code_tries) {
			if (step === "email") f.email_code = null; // burned: only a new code can do now
			else f.phone_code = null;
			this.touch(f);
			throw new SignupError("TOO_MANY_TRIES", 429, "Too many wrong tries — ask for a new code.");
		}
		this.touch(f);
		const left = p.code_tries - n;
		throw new SignupError("WRONG_CODE", 422, `That code is not right — ${left} ${left === 1 ? "try" : "tries"} left.`, { tries_left: left });
	}

	verifyEmail(id: string | null | undefined, code: unknown): View {
		const f = this.need(id);
		if (f.email_ok) return this.view(f); // a double submit: already done
		this.check(f, "email", code);
		f.email_ok = this.now();
		f.email_code = null;
		this.touch(f);
		return this.view(f);
	}

	/** Step 3: a number → its code by SMS. A new number restarts this step; the email stays verified. */
	async phone(id: string | null | undefined, input: unknown, ip: string, site: Site): Promise<View> {
		const f = this.need(id);
		if (!f.email_ok) throw new SignupError("OUT_OF_ORDER", 409, "Verify your email first.", { view: this.view(f) });
		const p = this.policy();
		const parsed = parsePhone(input, p.sms_countries);
		if (!parsed.ok) {
			if (parsed.why === "PHONE_COUNTRY") throw new SignupError("PHONE_COUNTRY", 422, `Text messages can't be sent to numbers of this country (${parsed.country}) from here.`);
			throw new SignupError("BAD_PHONE", 422, "Type the number with its country code, e.g. +40 712 345 678.");
		}
		if (f.phone_ok && f.phone === parsed.e164) return this.view(f);
		if (this.users.phoneOwner(parsed.e164)) throw new SignupError("PHONE_TAKEN", 409, "This number already belongs to an account.");
		this.stepLimits(f.phone_sends, "phone"); // across numbers: changing the number does not buy more texts
		this.smsLimits(parsed.e164, ip);
		await this.sendSms(f, parsed.e164, ip, site);
		return this.view(this.flows.get(f.id) || f);
	}

	changePhone(id: string | null | undefined): View {
		const f = this.need(id);
		if (!f.email_ok) throw new SignupError("OUT_OF_ORDER", 409, "Verify your email first.", { view: this.view(f) });
		f.phone = null;
		f.phone_code = null;
		f.phone_ok = null;
		f.phone_tries = 0;
		this.touch(f);
		return this.view(f);
	}

	verifyPhone(id: string | null | undefined, code: unknown): View {
		const f = this.need(id);
		if (!f.email_ok || !f.phone) throw new SignupError("OUT_OF_ORDER", 409, f.email_ok ? "Ask for a code to your phone first." : "Verify your email first.", { view: this.view(f) });
		if (f.phone_ok) return this.view(f);
		this.check(f, "phone", code);
		f.phone_ok = this.now();
		f.phone_code = null;
		this.touch(f);
		return this.view(f);
	}

	async resend(id: string | null | undefined, ip: string, site: Site): Promise<View> {
		const f = this.need(id);
		const step = this.step(f);
		if (step === "email_code") {
			this.stepLimits(f.email_sends, "email");
			this.emailLimits(f.email, ip);
			await this.sendEmail(f, ip, site);
		} else if (step === "phone_code" && f.phone) {
			this.stepLimits(f.phone_sends, "phone");
			this.smsLimits(f.phone, ip);
			await this.sendSms(f, f.phone, ip, site);
		} else {
			throw new SignupError("OUT_OF_ORDER", 409, "There is no code to send again at this step.", { view: this.view(f) });
		}
		return this.view(this.flows.get(f.id) || f);
	}

	/** Step 5: the password → the account, written as a member with the verified phone; the flow is used up. Checked
	 * and written in one synchronous block (scrypt included), so two requests of one flow cannot both get here. */
	finish(id: string | null | undefined, password: unknown, ip: string): string {
		const f = this.need(id);
		if (!f.email_ok || !f.phone_ok || !f.phone) throw new SignupError("OUT_OF_ORDER", 409, f.email_ok ? "Verify your phone first." : "Verify your email first.", { view: this.view(f) });
		const p = this.policy();
		const pw = typeof password === "string" ? password : "";
		if (pw.length < p.min_password) throw new SignupError("WEAK_PASSWORD", 422, `The password needs at least ${p.min_password} characters.`);
		if (pw.length > 200) throw new SignupError("WEAK_PASSWORD", 422, "The password is at most 200 characters.");
		if (pw.trim().toLowerCase() === f.email) throw new SignupError("WEAK_PASSWORD", 422, "The password can't be your email address.");
		const iso = (t: number) => new Date(t).toISOString().replace(/\.\d{3}Z$/, "+00:00");
		let name: string;
		try {
			name = this.users.create(f.email, { hash: hashPassword(pw), role: "member", phone: f.phone, verified: { email: iso(f.email_ok), phone: iso(f.phone_ok) }, via: "signup" });
		} catch (e) {
			if (e instanceof ResumesError && (e.code === "ACCOUNT_EXISTS" || e.code === "PHONE_TAKEN")) {
				if (e.code === "ACCOUNT_EXISTS") {
					this.flows.delete(f.id);
					this.saveFlows();
				}
				throw new SignupError(e.code, 409, e.message);
			}
			throw e;
		}
		this.flows.delete(f.id);
		this.saveFlows();
		this.write(this.note({ at: this.now(), kind: "account", to: name, ip, flow: f.id.slice(0, 8) }));
		console.log(`signup · account made: ${name} (${f.phone})`);
		return name;
	}

	cancel(id: string | null | undefined): void {
		if (id && this.flows.delete(id)) this.saveFlows();
	}
}

const esc = (s: string) => s.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]!);

function frame(body: string): string {
	return `<!doctype html><html><body style="margin:0;padding:24px;background:#f6f7f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#0f172a">
<div style="max-width:440px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:28px">
<div style="font-size:13px;font-weight:600;color:#0f766e;margin-bottom:18px">${esc(PRODUCT)}</div>
${body}
</div></body></html>`;
}

// the code email has no links and no buttons
export function codeEmail(code: string, ttlSeconds: number): { subject: string; text: string; html: string } {
	const minutes = Math.round(ttlSeconds / 60);
	return {
		subject: `${code} is your ${PRODUCT} code`,
		text: `Your ${PRODUCT} code is ${code}\n\nType it on the sign-up page. It expires in ${minutes} minutes.\n\nIf you didn't ask for it, ignore this email: no account is made without it.\n`,
		html: frame(`<p style="margin:0 0 12px;font-size:14px">Your sign-up code:</p>
<div style="font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:34px;font-weight:700;letter-spacing:6px;margin:0 0 16px">${esc(code)}</div>
<p style="margin:0 0 10px;font-size:13px;color:#475569">Type it on the sign-up page. It expires in ${minutes} minutes.</p>
<p style="margin:0;font-size:12px;color:#64748b">If you didn't ask for it, ignore this email: no account is made without it.</p>`),
	};
}

export function knownEmail(site: Site): { subject: string; text: string; html: string } {
	const url = `${site.https ? "https" : "http"}://${site.host}`;
	return {
		subject: `You already have a ${PRODUCT} account`,
		text: `Someone (hopefully you) tried to sign up to ${PRODUCT} with this address, but it already has an account.\n\nLog in at ${url}\n\nIf it wasn't you, ignore this email: nothing has changed.\n`,
		html: frame(`<p style="margin:0 0 12px;font-size:14px">Someone (hopefully you) tried to sign up with this address, but it already has an account.</p>
<p style="margin:0 0 16px;font-size:14px">Log in at <b>${esc(url)}</b></p>
<p style="margin:0;font-size:12px;color:#64748b">If it wasn't you, ignore this email: nothing has changed.</p>`),
	};
}
