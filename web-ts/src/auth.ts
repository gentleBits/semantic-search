/**
 * The login: the users file (`.resumes/users.json`, scrypt hashes), the HMAC-signed login cookie, and lockouts after
 * wrong passwords. A record without a role is an admin; a blocked one cannot log in, but its email and phone stay taken.
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
	hash: string;
	created: string | null;
	role: Role; // absent in the file means admin (every `resumes users add`)
	phone: string | null;
}

export class Users {
	private seen = -1;
	private map = new Map<string, UserRecord>();
	private phones = new Map<string, string>();

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
			if (!key || !rec || typeof rec.hash !== "string" || !rec.hash.startsWith("scrypt$")) continue;
			const phone = typeof rec.phone === "string" && rec.phone ? rec.phone : null;
			this.map.set(key, { hash: rec.hash, created: typeof rec.created === "string" ? rec.created : null, role: ROLES.has(rec.role) ? rec.role : "admin", phone });
			if (phone) this.phones.set(phone, key);
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

	phoneOwner(phone: string): string | null {
		this.load();
		return this.phones.get(phone) ?? null;
	}

	names(): string[] {
		this.load();
		return [...this.map.keys()].sort();
	}

	verify(name: string, password: string): boolean {
		this.load();
		const rec = this.map.get(normalizeName(name));
		if (!rec || rec.role === "blocked") {
			verifyPassword(password, rec?.hash || DECOY); // the same time whether the name exists, is blocked, or not
			return false;
		}
		return verifyPassword(password, rec.hash);
	}

	/** Under the lock `resumes users …` takes too: name and phone are re-checked inside it. Never replaces an account. */
	create(name: string, rec: { hash: string; role: Role; phone?: string | null; verified?: Record<string, string>; via?: string }): string {
		const key = normalizeName(name);
		if (!key || /\s/.test(key) || key.length > 200) throw new ResumesError("BAD_ARGUMENT", "the account name is one word, e.g. an email address");
		return withLock(join(dirname(this.path), USERS_LOCK), () => {
			let raw: any = null;
			try {
				raw = JSON.parse(readFileSync(this.path, "utf-8"));
			} catch (e) {
				if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw new ResumesError("BUSY", `${this.path} cannot be read: ${(e as Error).message}`);
			}
			const users: Record<string, any> = raw && typeof raw === "object" && raw.users && typeof raw.users === "object" ? raw.users : {};
			for (const [n, r] of Object.entries(users)) {
				if (normalizeName(n) === key) throw new ResumesError("ACCOUNT_EXISTS", "There is already an account with this email — log in instead.");
				if (rec.phone && r && (r as any).phone === rec.phone) throw new ResumesError("PHONE_TAKEN", "This number already belongs to an account.");
			}
			users[key] = {
				hash: rec.hash,
				created: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
				role: rec.role,
				...(rec.phone ? { phone: rec.phone } : {}),
				...(rec.verified ? { verified: rec.verified } : {}),
				via: rec.via || "signup",
			};
			writeJsonAtomic(this.path, { users });
			this.seen = -1;
			return key;
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
