/**
 * The sign-up's senders: an email by Resend, an SMS by Sakari (keys from the environment only), or a dry run
 * (`RESUMES_SIGNUP_DRYRUN=1`) that prints each message instead.
 */

export type Http = (url: string, init: { method: string; headers: Record<string, string>; body?: string }) => Promise<{ status: number; text: string }>;

export const TIMEOUT_MS = 15_000;

export const httpFetch: Http = async (url, init) => {
	const res = await fetch(url, { ...init, signal: AbortSignal.timeout(TIMEOUT_MS) });
	return { status: res.status, text: await res.text() };
};

/** The provider could not be reached, or refused the message: nothing was sent. */
export class SendError extends Error {
	constructor(public provider: string, message: string) {
		super(message);
	}
}

export interface Email {
	to: string;
	subject: string;
	text: string;
	html: string;
	key: string; // idempotency: the same key is the same email, sent once
}

export interface Senders {
	readonly name: string;
	email(m: Email): Promise<{ id: string | null }>;
	sms(to: string, text: string): Promise<{ id: string | null; invalid: boolean }>;
}

const json = (text: string): any => {
	try {
		return text ? JSON.parse(text) : null;
	} catch {
		return null;
	}
};

const said = (text: string): string => {
	const j = json(text);
	const m = j && (j.message || j.error?.message || j.error || j.name);
	return String(typeof m === "string" ? m : text || "").slice(0, 200);
};

export class Resend {
	constructor(public apiKey: string, public from: string, public http: Http = httpFetch, public base = "https://api.resend.com") {}

	async send(m: Email): Promise<{ id: string | null }> {
		let r: { status: number; text: string };
		try {
			r = await this.http(`${this.base}/emails`, {
				method: "POST",
				headers: { Authorization: `Bearer ${this.apiKey}`, "Content-Type": "application/json", "Idempotency-Key": m.key },
				body: JSON.stringify({ from: this.from, to: [m.to], subject: m.subject, text: m.text, html: m.html }),
			});
		} catch (e) {
			throw new SendError("resend", `Resend cannot be reached: ${(e as Error).message}`);
		}
		if (r.status < 200 || r.status >= 300) throw new SendError("resend", `Resend answered ${r.status}: ${said(r.text)}`);
		const id = json(r.text)?.id;
		return { id: typeof id === "string" ? id : null };
	}
}

/** A client-credentials token, kept until 30 s before it expires; a 401/403 forgets it and the send is tried once more. */
export class Sakari {
	private token: { value: string; until: number } | null = null;
	private fetching: Promise<string> | null = null;

	constructor(
		public accountId: string,
		public clientId: string,
		public clientSecret: string,
		public http: Http = httpFetch,
		public base = "https://api.sakari.io",
		public now: () => number = () => Date.now(),
	) {}

	private async accessToken(): Promise<string> {
		if (this.token && this.token.until - this.now() > 30_000) return this.token.value;
		if (this.fetching) return this.fetching;
		this.fetching = (async () => {
			try {
				let r: { status: number; text: string };
				try {
					r = await this.http(`${this.base}/oauth2/token`, {
						method: "POST",
						headers: { "Content-Type": "application/x-www-form-urlencoded" },
						body: new URLSearchParams({ grant_type: "client_credentials", client_id: this.clientId, client_secret: this.clientSecret }).toString(),
					});
				} catch (e) {
					throw new SendError("sakari", `Sakari cannot be reached: ${(e as Error).message}`);
				}
				const j = json(r.text);
				if (r.status < 200 || r.status >= 300 || !j || typeof j.access_token !== "string") throw new SendError("sakari", `Sakari refused the token (${r.status}): ${said(r.text)}`);
				this.token = { value: j.access_token, until: this.now() + (Number(j.expires_in) || 3600) * 1000 };
				return this.token.value;
			} finally {
				this.fetching = null;
			}
		})();
		return this.fetching;
	}

	async send(to: string, text: string): Promise<{ id: string | null; invalid: boolean }> {
		for (let attempt = 0; ; attempt++) {
			const token = await this.accessToken();
			let r: { status: number; text: string };
			try {
				r = await this.http(`${this.base}/v1/accounts/${encodeURIComponent(this.accountId)}/messages`, {
					method: "POST",
					headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
					body: JSON.stringify({ contacts: [{ mobile: to }], template: text }),
				});
			} catch (e) {
				throw new SendError("sakari", `Sakari cannot be reached: ${(e as Error).message}`);
			}
			if ((r.status === 401 || r.status === 403) && attempt === 0) {
				this.token = null; // a token Sakari no longer takes: one more try with a fresh one (nothing was sent)
				continue;
			}
			if (r.status < 200 || r.status >= 300) throw new SendError("sakari", `Sakari answered ${r.status}: ${said(r.text)}`);
			const data = json(r.text)?.data ?? {};
			const messages = Array.isArray(data.messages) ? data.messages : [];
			// undeliverable only on Sakari's explicit word: any other 2xx counts as sent, so nobody sends (and pays) twice
			const invalid = (Array.isArray(data.invalid) && data.invalid.length > 0) || data.valid === 0;
			const id = messages[0]?.id;
			return { id: typeof id === "string" ? id : null, invalid };
		}
	}
}

export class Providers implements Senders {
	readonly name = "resend + sakari";
	constructor(public resend: Resend, public sakari: Sakari) {}
	email(m: Email) {
		return this.resend.send(m);
	}
	sms(to: string, text: string) {
		return this.sakari.send(to, text);
	}
}

/** Nothing leaves the machine: each message is printed (codes included) and kept in `sent` (tests, a walk through the page). */
export class DryRun implements Senders {
	readonly name = "dry run";
	sent: { kind: "email" | "sms"; to: string; text: string; subject?: string }[] = [];
	constructor(public print = true) {}
	async email(m: Email) {
		this.sent.push({ kind: "email", to: m.to, text: m.text, subject: m.subject });
		if (this.print) console.log(`signup · dry run · email to ${m.to}: ${m.subject}`);
		return { id: `dry-${this.sent.length}` };
	}
	async sms(to: string, text: string) {
		this.sent.push({ kind: "sms", to, text });
		if (this.print) console.log(`signup · dry run · SMS to ${to}: ${text.split("\n")[0]}`);
		return { id: `dry-${this.sent.length}`, invalid: false };
	}
}

export const PROVIDER_ENV = ["RESEND_API_KEY", "RESEND_FROM", "SAKARI_ACCOUNT_ID", "SAKARI_CLIENT_ID", "SAKARI_CLIENT_SECRET"] as const;

/** The dry run when asked for, else the providers when every key is there, else null and the missing keys (sign-up
 * then stays off). */
export function sendersFrom(env: Record<string, string | undefined>, http: Http = httpFetch): { senders: Senders | null; missing: string[] } {
	if (["1", "true", "yes"].includes(String(env.RESUMES_SIGNUP_DRYRUN || "").toLowerCase())) return { senders: new DryRun(), missing: [] };
	const missing = PROVIDER_ENV.filter((k) => !String(env[k] || "").trim());
	if (missing.length) return { senders: null, missing: [...missing] };
	const v = (k: string) => String(env[k]).trim();
	return {
		senders: new Providers(
			new Resend(v("RESEND_API_KEY"), v("RESEND_FROM"), http),
			new Sakari(v("SAKARI_ACCOUNT_ID"), v("SAKARI_CLIENT_ID"), v("SAKARI_CLIENT_SECRET"), http, String(env.SAKARI_API_BASE || "").trim() || "https://api.sakari.io"),
		),
		missing: [],
	};
}
