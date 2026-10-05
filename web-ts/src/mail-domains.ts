/**
 * Which addresses may begin a sign-up, judged by the domain alone so that a refusal tells nothing about an account: no
 * throwaway domain, no free provider while work_email is on, and an MX that exists and is not a throwaway service's.
 */
import { promises as dns } from "node:dns";
import { readFileSync } from "node:fs";

export type MailVerdict = { ok: true } | { ok: false; why: "DISPOSABLE_EMAIL" | "PERSONAL_EMAIL" | "NO_MAIL"; domain: string };

/** A domain's mail servers; "none" when it has none (no such domain, no MX and no address, or a null MX); "unknown"
 * when DNS did not say (a timeout, a server failure). */
export type MxAnswer = { hosts: string[] } | "none" | "unknown";
export type MxLookup = (domain: string) => Promise<MxAnswer>;

const DATA = new URL("../data/", import.meta.url); // web-ts/data, from src/ (tsx) as from dist/
const MX_TTL = 3600_000;
const MX_CACHE_MAX = 5000;

function readList(name: string): Set<string> {
	const out = new Set<string>();
	for (const line of readFileSync(new URL(name, DATA), "utf-8").split("\n")) {
		const d = line.replace(/#.*/, "").trim().toLowerCase();
		if (d) out.add(d);
	}
	return out;
}

export interface MailLists {
	disposable: Set<string>;
	free: Set<string>;
}

let lists: MailLists | null = null;
export function mailLists(): MailLists {
	if (!lists) lists = { disposable: readList("disposable-domains.txt"), free: readList("free-domains.txt") };
	return lists;
}

/** The domain of `host` (or the parent of it) that `set` holds, or null. */
export function listed(set: Set<string>, host: string): string | null {
	const labels = host.toLowerCase().replace(/\.$/, "").split(".");
	for (let i = 0; i < labels.length - 1; i++) {
		const d = labels.slice(i).join(".");
		if (set.has(d)) return d;
	}
	return null;
}

/** The system's resolver, with a short timeout: a sign-up waits on it. */
export function systemMx(timeoutMs = 2500): MxLookup {
	const r = new dns.Resolver({ timeout: timeoutMs, tries: 1 }); // one try: a silent DNS lets the address through anyway
	const code = (e: unknown) => (e as { code?: string })?.code;
	return async (domain) => {
		try {
			const mx = await r.resolveMx(domain);
			const hosts = mx.filter((m) => m.exchange && m.exchange !== ".").map((m) => m.exchange.toLowerCase());
			if (hosts.length) return { hosts };
			if (mx.length) return "none"; // only a null MX (RFC 7505): "this domain takes no mail"
		} catch (e) {
			if (code(e) === "ENOTFOUND") return "none"; // no such domain
			if (code(e) !== "ENODATA") return "unknown";
		}
		// no MX: mail goes to the domain's own address (RFC 5321, section 5.1)
		for (const look of [r.resolve4.bind(r), r.resolve6.bind(r)]) {
			try {
				if ((await look(domain)).length) return { hosts: [domain] };
			} catch (e) {
				if (code(e) !== "ENODATA" && code(e) !== "ENOTFOUND") return "unknown";
			}
		}
		return "none";
	};
}

export class MailDomains {
	private cache = new Map<string, { at: number; answer: MxAnswer }>();

	/** `mx` null: no DNS look (the dry run, which mails nothing). */
	constructor(public mx: MxLookup | null, public lists: () => MailLists = mailLists, public now: () => number = () => Date.now()) {}

	async check(email: string, workOnly: boolean): Promise<MailVerdict> {
		const domain = email.slice(email.lastIndexOf("@") + 1).toLowerCase();
		const { disposable, free } = this.lists();
		if (listed(disposable, domain)) return { ok: false, why: "DISPOSABLE_EMAIL", domain };
		if (workOnly && listed(free, domain)) return { ok: false, why: "PERSONAL_EMAIL", domain };
		if (!this.mx) return { ok: true };
		const answer = await this.lookup(domain);
		if (answer === "none") return { ok: false, why: "NO_MAIL", domain };
		if (answer !== "unknown" && answer.hosts.some((h) => listed(disposable, h))) return { ok: false, why: "DISPOSABLE_EMAIL", domain };
		return { ok: true };
	}

	private async lookup(domain: string): Promise<MxAnswer> {
		const hit = this.cache.get(domain);
		if (hit && this.now() - hit.at < MX_TTL) return hit.answer;
		const answer = await this.mx!(domain);
		if (answer !== "unknown") {
			if (this.cache.size >= MX_CACHE_MAX) this.cache.clear();
			this.cache.set(domain, { at: this.now(), answer });
		}
		return answer;
	}
}
