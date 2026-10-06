/**
 * Who may search: the users file (`.resumes/users.json`), the HMAC-signed cookie, and lockouts after wrong passwords.
 * A record is made by `resumes users add` (an admin, with a password) or by the email and phone check (a member, no
 * password). A record without a role is an admin; a blocked one is let in no more, and its numbers with it.
 */
import { createHmac, randomBytes, scryptSync, timingSafeEqual } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { Context } from "hono";
import { deleteCookie, getCookie, setCookie } from "hono/cookie";
import { ResumesError } from "./errors.js";
import { withLock, writeJsonAtomic } from "./files.js";

export const USERS_FILE = "users.json";
export const USERS_LOCK = "users.lock";
export type Role = "admin" | "member" | "blocked";
const ROLES = new Set(["admin", "member", "blocked"]);
export const SECRET_FILE = "secret";
export const COOKIE = "resumes_login";
export const TTL_SECONDS = 30 * 24 * 3600;
export const LOCK_AFTER = 5; // wrong passwords in a row …
export const LOCK_SECONDS = 60; // … lock the name for this long

const b64 = (b: Buffer) => b.toString("base64url");
const unb64 = (s: string) => Buffer.from(s, "base64url");

/** `scrypt$<log2 N>$<r>$<p>$<salt>$<hash>`, byte-compatible with Python's hashlib.scrypt in `resumes users add`. */
export function hashPassword(password: string, opts: { logN?: number; r?: number; p?: number; salt?: Buffer } = {}): string {
	const logN = opts.logN ?? 14;
	const r = opts.r ?? 8;
	const p = opts.p ?? 1;
	const salt = opts.salt ?? randomBytes(16);
	const hash = scryptSync(Buffer.from(password, "utf-8"), salt, 32, { N: 2 ** logN, r, p });
	return `scrypt$${logN}$${r}$${p}$${b64(salt)}$${b64(hash)}`;
}

export function verifyPassword(password: string, stored: string): boolean {
	const parts = String(stored || "").split("$");
	if (parts.length !== 6 || parts[0] !== "scrypt") return false;
	const [logN, r, p] = parts.slice(1, 4).map((x) => Number(x));
	if (![logN, r, p].every((n) => Number.isInteger(n) && n > 0) || logN > 20) return false;
	let want: Buffer;
	let got: Buffer;
	try {
		want = unb64(parts[5]);
		got = scryptSync(Buffer.from(password, "utf-8"), unb64(parts[4]), want.length, { N: 2 ** logN, r, p });
	} catch {
		return false;
	}
	return want.length > 0 && want.length === got.length && timingSafeEqual(want, got);
}

export const normalizeName = (name: unknown): string => String(name ?? "").trim().toLowerCase();

export interface UserRecord {
	hash: string | null; // a password: only the operator's accounts (`resumes users add`) have one
	created: string | null;
	role: Role; // absent in the file means admin (every `resumes users add`)
	phone: string | null; // the number this person came through with last
}

/** What the check passed: a member's number is theirs from now on; `texted` when it was verified by a code just now. */
export interface Passed {
	phone: string;
	texted: boolean;
}

const numbersOf = (rec: any): string[] => {
	const all = [...(Array.isArray(rec?.phones) ? rec.phones : []), rec?.phone];
	return [...new Set(all.filter((x): x is string => typeof x === "string" && /^\+\d{7,15}$/.test(x)))];
};

export class Users {
	private seen = -1;
	private map = new Map<string, UserRecord>();
	private phones = new Map<string, string>(); // a number verified once → a person who came through with it (not blocked)
	private blockedPhones = new Set<string>();
	private byPhone = new Map<string, Set<string>>(); // a number → the people whose last number it is

	constructor(public path: string) {}

	private load(): void {
		let stamp = 0;
		try {
			const s = statSync(this.path);
			stamp = s.mtimeMs + s.size / 1e9; // a rewrite within the same millisecond still differs in size, usually
		} catch {
			stamp = 0;
		}
		if (stamp === this.seen) return;
		this.seen = stamp;
		this.map = new Map();
		this.phones = new Map();
		this.blockedPhones = new Set();
		this.byPhone = new Map();
		if (!stamp) return;
		let raw: any = null;
		try {
			raw = JSON.parse(readFileSync(this.path, "utf-8"));
		} catch {
			raw = null;
		}
		const users = raw && typeof raw === "object" && raw.users && typeof raw.users === "object" ? raw.users : {};
		for (const [name, rec] of Object.entries(users as Record<string, any>)) {
			const key = normalizeName(name);
			if (!key || !rec || typeof rec !== "object") continue;
			const hash = typeof rec.hash === "string" && rec.hash.startsWith("scrypt$") ? rec.hash : null;
			const role: Role = ROLES.has(rec.role) ? rec.role : "admin";
			if (!hash && role === "admin") continue; // an operator's account is a password; without one it is nothing
			const phone = typeof rec.phone === "string" && rec.phone ? rec.phone : null;
			this.map.set(key, { hash, created: typeof rec.created === "string" ? rec.created : null, role, phone });
			for (const n of numbersOf(rec)) {
				if (role === "blocked") this.blockedPhones.add(n);
				else if (!this.phones.has(n)) this.phones.set(n, key);
			}
			if (phone && role !== "blocked") this.byPhone.set(phone, (this.byPhone.get(phone) || new Set()).add(key));
		}
	}

	count(): number {
		this.load();
		return this.map.size;
	}

	/** An account that may log in (not blocked). */
	has(name: string): boolean {
		this.load();
		const rec = this.map.get(normalizeName(name));
		return !!rec && rec.role !== "blocked";
	}

	/** The name is taken: an account of any role, blocked ones too. */
	taken(name: string): boolean {
		this.load();
		return this.map.has(normalizeName(name));
	}

	role(name: string): Role | null {
		this.load();
		return this.map.get(normalizeName(name))?.role ?? null;
	}

	phone(name: string): string | null {
		this.load();
		return this.map.get(normalizeName(name))?.phone ?? null;
	}

	/** A number someone came through with before (verified by a text once): it needs no code again. */
	knownPhone(phone: string): boolean {
		this.load();
		return this.phones.has(phone) && !this.blockedPhones.has(phone);
	}

	/** A number of a blocked person: let in with no address. */
	blockedPhone(phone: string): boolean {
		this.load();
		return this.blockedPhones.has(phone);
	}

	/** Everyone who came through with this person's number last, the person included: they share one allowance. */
	sharing(name: string): string[] {
		this.load();
		const key = normalizeName(name);
		const phone = this.map.get(key)?.phone;
		return phone ? [...(this.byPhone.get(phone) || [key])] : [key];
	}

	names(): string[] {
		this.load();
		return [...this.map.keys()].sort();
	}

	verify(name: string, password: string): boolean {
		this.load();
		const rec = this.map.get(normalizeName(name));
		if (!rec || rec.role === "blocked" || !rec.hash) {
			verifyPassword(password, rec?.hash || DECOY); // the same time whether the name exists, is blocked, or not
			return false;
		}
		return verifyPassword(password, rec.hash);
	}

	/** The check passed for this address: a new member, or the record brought up to date (the number they came with,
	 * when it was verified). Under the lock `resumes users …` takes too; an operator's account keeps its role, its
	 * password and its numbers, and a blocked one is refused (BLOCKED) — the caller sent it no code, so it cannot get here. */
	pass(name: string, how: Passed, now = new Date()): { name: string; made: boolean } {
		const key = normalizeName(name);
		if (!key || /\s/.test(key) || key.length > 200) throw new ResumesError("BAD_ARGUMENT", "the name is one word, e.g. an email address");
		const iso = now.toISOString().replace(/\.\d{3}Z$/, "+00:00");
		return withLock(join(dirname(this.path), USERS_LOCK), () => {
			let raw: any = null;
			try {
				raw = JSON.parse(readFileSync(this.path, "utf-8"));
			} catch (e) {
				if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw new ResumesError("BUSY", `${this.path} cannot be read: ${(e as Error).message}`);
			}
			const users: Record<string, any> = raw && typeof raw === "object" && raw.users && typeof raw.users === "object" ? raw.users : {};
			const found = Object.keys(users).find((n) => normalizeName(n) === key);
			const rec: Record<string, any> = found ? { ...users[found] } : { created: iso, role: "member", via: "check" };
			if (found && found !== key) delete users[found];
			const role: Role = ROLES.has(rec.role) ? rec.role : "admin";
			if (role === "blocked") throw new ResumesError("BLOCKED", "this address is blocked");
			if (role === "admin" && !(typeof rec.hash === "string")) throw new ResumesError("BLOCKED", "an operator's account without a password");
			rec.verified = { ...(rec.verified && typeof rec.verified === "object" ? rec.verified : {}), email: iso, ...(how.texted ? { phone: iso } : {}) };
			rec.seen = iso;
			if (role === "member") {
				rec.phone = how.phone;
				rec.phones = [...new Set([...numbersOf(rec), how.phone])];
			}
			users[key] = rec;
			writeJsonAtomic(this.path, { users });
			this.seen = -1;
			return { name: key, made: !found };
		});
	}
}

const DECOY = hashPassword("decoy", { salt: Buffer.alloc(16, 7) });

/** Wrong passwords from one address: this many in the window … */
export interface LoginLimits {
	login_ip_fails: number;
	login_ip_window: number; // seconds
	login_ip_wait: number; // … and the address waits this long
}
export const LOGIN_LIMITS: LoginLimits = { login_ip_fails: 20, login_ip_window: 600, login_ip_wait: 600 };

export class Auth {
	readonly users: Users;
	private secretBytes: Buffer | null = null;
	private fails = new Map<string, { n: number; until: number }>();
	private ipFails = new Map<string, { at: number[]; until: number }>();

	constructor(
		public stateDir: string,
		public now: () => number = () => Date.now(),
		public signupOn: () => boolean = () => false,
		public limits: () => LoginLimits = () => LOGIN_LIMITS,
	) {
		this.users = new Users(join(stateDir, USERS_FILE));
	}

	/** Also on while sign-up is on, so the first visitor of a fresh server is never let in unchecked. */
	on(): boolean {
		return this.users.count() > 0 || this.signupOn();
	}

	key(): Buffer {
		return this.secret();
	}

	private secret(): Buffer {
		if (this.secretBytes) return this.secretBytes;
		const path = join(this.stateDir, SECRET_FILE);
		let hex = "";
		try {
			hex = readFileSync(path, "utf-8").trim();
		} catch {
			hex = "";
		}
		if (!/^[0-9a-f]{64}$/.test(hex)) {
			hex = randomBytes(32).toString("hex");
			mkdirSync(dirname(path), { recursive: true });
			const tmp = `${path}.${process.pid}.tmp`;
			writeFileSync(tmp, `${hex}\n`, { mode: 0o600 });
			try {
				chmodSync(tmp, 0o600);
			} catch {
				// a file system without modes
			}
			renameSync(tmp, path);
		}
		this.secretBytes = Buffer.from(hex, "hex");
		return this.secretBytes;
	}

	private sign(payload: string): string {
		return createHmac("sha256", this.secret()).update(payload).digest("base64url");
	}

	/** `<name>.<expiry>.<signature>`, the name base64url so that the token is one cookie-safe word. */
	token(name: string, ttl = TTL_SECONDS): string {
		const exp = Math.floor(this.now() / 1000) + ttl;
		const payload = `${Buffer.from(normalizeName(name), "utf-8").toString("base64url")}.${exp}`;
		return `${payload}.${this.sign(payload)}`;
	}

	/** The name a token stands for, or null: a bad signature, an expiry passed, a user since removed. */
	read(token: string | undefined): string | null {
		if (!token) return null;
		const parts = token.split(".");
		if (parts.length !== 3) return null;
		const [nameB64, expText, sig] = parts;
		const exp = Number(expText);
		if (!/^\d+$/.test(expText) || exp * 1000 <= this.now()) return null;
		const want = Buffer.from(this.sign(`${nameB64}.${expText}`));
		const got = Buffer.from(sig);
		if (want.length !== got.length || !timingSafeEqual(want, got)) return null;
		let name = "";
		try {
			name = normalizeName(Buffer.from(nameB64, "base64url").toString("utf-8"));
		} catch {
			return null;
		}
		return name && this.users.has(name) ? name : null;
	}

	who(c: Context): string | null {
		return this.on() ? this.read(getCookie(c, COOKIE)) : null;
	}

	lockedFor(name: string): number {
		const f = this.fails.get(normalizeName(name));
		if (!f || f.n < LOCK_AFTER) return 0;
		const left = Math.ceil((f.until - this.now()) / 1000);
		if (left <= 0) {
			this.fails.delete(normalizeName(name));
			return 0;
		}
		return left;
	}

	ipLockedFor(ip: string): number {
		const f = this.ipFails.get(ip);
		if (!f || !f.until) return 0;
		const left = Math.ceil((f.until - this.now()) / 1000);
		if (left <= 0) {
			this.ipFails.delete(ip);
			return 0;
		}
		return left;
	}

	private ipFailed(ip: string): void {
		const l = this.limits();
		const now = this.now();
		const f = this.ipFails.get(ip) || { at: [], until: 0 };
		f.at = f.at.filter((t) => now - t < l.login_ip_window * 1000);
		f.at.push(now);
		if (f.at.length >= l.login_ip_fails) f.until = now + l.login_ip_wait * 1000;
		this.ipFails.set(ip, f);
		if (this.ipFails.size > 10_000) for (const [k, v] of this.ipFails) if (!v.until && !v.at.some((t) => now - t < l.login_ip_window * 1000)) this.ipFails.delete(k);
	}

	setLogin(c: Context, name: string): void {
		setCookie(c, COOKIE, this.token(name), { path: "/", httpOnly: true, sameSite: "Lax", maxAge: TTL_SECONDS, secure: isHttps(c) });
	}

	login(c: Context, name: string, password: string, ip = ""): string | null {
		const key = normalizeName(name);
		if (!key || !password) return null;
		if (this.users.verify(key, password)) {
			this.fails.delete(key);
			this.setLogin(c, key);
			return key;
		}
		const f = this.fails.get(key) || { n: 0, until: 0 };
		f.n += 1;
		if (f.n >= LOCK_AFTER) f.until = this.now() + LOCK_SECONDS * 1000;
		this.fails.set(key, f);
		if (ip) this.ipFailed(ip);
		return null;
	}

	logout(c: Context): void {
		deleteCookie(c, COOKIE, { path: "/" });
	}
}

/** Over TLS itself, or behind a proxy that says so. */
export function isHttps(c: Context): boolean {
	if (c.req.url.startsWith("https:")) return true;
	const forwarded = (c.req.header("x-forwarded-proto") || "").split(",")[0].trim().toLowerCase();
	return forwarded === "https";
}

export function usersFileExists(stateDir: string): boolean {
	return existsSync(join(stateDir, USERS_FILE));
}
