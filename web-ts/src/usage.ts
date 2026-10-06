/**
 * What a member may spend on the assistant: a day's turns, model cost and new sessions, and one turn at a time
 * (`.resumes/usage.jsonl`, `.resumes/sessions-made.jsonl`). Admins, and everyone while login is off, have no limit.
 * An unpriced model reports $0, which is why turns are counted too. The allowance is the phone number's: everyone who
 * came through the check with the same number shares one (`sharing`), so more addresses buy no more.
 */
import { appendFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { writeTextAtomic } from "./files.js";
import type { UsePolicy } from "./policy.js";
import { SignupError } from "./signup.js";
import type { Usage } from "./text.js";

export const USAGE_FILE = "usage.jsonl";
export const SESSIONS_FILE = "sessions-made.jsonl";
const DAY = 24 * 3600_000;

/** 3 h 20 min, 12 min, 1 min: how long until the allowance is back (the reader's clock may be anywhere). */
export function inWords(ms: number): string {
	const min = Math.max(1, Math.ceil(ms / 60_000));
	const h = Math.floor(min / 60);
	const m = min % 60;
	return h ? `${h} h${m ? ` ${m} min` : ""}` : `${m} min`;
}

interface Line {
	at: number;
	user: string;
	sid: string;
	cost: number;
	tokens: number;
	model: string;
}

interface Made {
	at: number;
	user: string;
	sid: string;
}

function readLines(path: string): any[] {
	let text = "";
	try {
		text = readFileSync(path, "utf-8");
	} catch {
		return [];
	}
	const out: any[] = [];
	for (const raw of text.split("\n")) {
		if (!raw) continue;
		try {
			out.push(JSON.parse(raw));
		} catch {
			// a broken line is skipped
		}
	}
	return out;
}

export class UsageBook {
	private lines: Line[] = []; // the last 24 h
	private running = new Map<string, number>(); // user → turns running now
	private made: Made[] = []; // new sessions, the last 24 h
	readonly path: string;
	readonly sessionsPath: string;

	constructor(
		public stateDir: string,
		public policy: () => UsePolicy,
		public now: () => number = () => Date.now(),
		public sharing: (user: string) => string[] = (user) => [user],
	) {
		this.path = join(stateDir, USAGE_FILE);
		this.sessionsPath = join(stateDir, SESSIONS_FILE);
		const since = this.now() - DAY;
		for (const l of readLines(this.path)) {
			if (typeof l.at === "number" && l.at >= since && typeof l.user === "string") this.lines.push({ ...l, cost: Number(l.cost) || 0 });
		}
		const lines = readLines(this.sessionsPath);
		for (const l of lines) if (typeof l.at === "number" && l.at >= since && typeof l.user === "string") this.made.push(l);
		if (lines.length > this.made.length) {
			// the older lines go: nothing reads them
			try {
				writeTextAtomic(this.sessionsPath, this.made.map((m) => `${JSON.stringify(m)}\n`).join(""));
			} catch (e) {
				console.error(`usage · ${this.sessionsPath} could not be rewritten: ${(e as Error).message}`);
			}
		}
	}

	/** One allowance: the people of one number, by a name that does not change while they share it. */
	private group(user: string): { key: string; names: Set<string> } {
		const names = new Set(this.sharing(user));
		names.add(user);
		return { key: [...names].sort()[0], names };
	}

	private recent(user: string): Line[] {
		const since = this.now() - DAY;
		if (this.lines.length && this.lines[0].at < since) this.lines = this.lines.filter((l) => l.at >= since);
		const { names } = this.group(user);
		return this.lines.filter((l) => names.has(l.user));
	}

	day(user: string): { turns: number; cost: number } {
		const r = this.recent(user);
		return { turns: r.length, cost: Math.round(r.reduce((s, l) => s + l.cost, 0) * 1e6) / 1e6 };
	}

	/** Throws BUSY or USAGE_LIMIT for a limited person; else counts the turn as running until the returned `done`. */
	begin(user: string | null, limited: boolean): () => void {
		const key = user ? this.group(user).key : "";
		if (limited && user) {
			const p = this.policy();
			if ((this.running.get(key) || 0) >= p.at_once) {
				throw new SignupError("BUSY", 409, "Your assistant is still working on another search — wait for it, or stop it there.");
			}
			const r = this.recent(user);
			const cost = r.reduce((s, l) => s + l.cost, 0);
			if (r.length >= p.turns_per_day || cost >= p.usd_per_day) {
				const back = this.reopensAt(r, p);
				throw new SignupError(
					"USAGE_LIMIT",
					429,
					`You've used today's assistant allowance — it's back in ${inWords(back - this.now())}. The list, the filters and the slash commands keep working.`,
					{ back_at: new Date(back).toISOString(), turns: r.length, cost: Math.round(cost * 1e4) / 1e4 },
				);
			}
		}
		this.running.set(key, (this.running.get(key) || 0) + 1);
		let done = false;
		return () => {
			if (done) return;
			done = true;
			const n = (this.running.get(key) || 1) - 1;
			if (n <= 0) this.running.delete(key);
			else this.running.set(key, n);
		};
	}

	/** When the allowance opens again: the moment enough of the oldest turns leave the 24-hour window. */
	private reopensAt(r: Line[], p: UsePolicy): number {
		const sorted = [...r].sort((a, b) => a.at - b.at);
		let turns = sorted.length;
		let cost = sorted.reduce((s, l) => s + l.cost, 0);
		for (const l of sorted) {
			if (turns < p.turns_per_day && cost < p.usd_per_day) break;
			turns -= 1;
			cost -= l.cost;
			if (turns < p.turns_per_day && cost < p.usd_per_day) return l.at + DAY;
		}
		return (sorted[0]?.at ?? this.now()) + DAY;
	}

	private madeBy(user: string): Made[] {
		const since = this.now() - DAY;
		if (this.made.length && this.made[0].at < since) this.made = this.made.filter((m) => m.at >= since);
		const { names } = this.group(user);
		return this.made.filter((m) => names.has(m.user));
	}

	sessionsToday(user: string): number {
		return this.madeBy(user).length;
	}

	/** Throws SESSION_LIMIT; one with no session left may always start one: the page needs a session to open on. */
	mayStartSession(user: string, hasNone: boolean): void {
		const p = this.policy();
		if (hasNone || this.sessionsToday(user) < p.sessions_per_day) return;
		const mine = this.madeBy(user);
		const back = (mine[mine.length - p.sessions_per_day]?.at ?? this.now()) + DAY;
		throw new SignupError(
			"SESSION_LIMIT",
			429,
			`You have reached the limit of ${p.sessions_per_day} new sessions per day; you can start another in ${inWords(back - this.now())}.`,
			{ back_at: new Date(back).toISOString() },
		);
	}

	/** Everyone's: the limit is checked for members only. */
	sessionMade(user: string, sid: string): void {
		const m: Made = { at: this.now(), user, sid };
		this.made.push(m);
		try {
			appendFileSync(this.sessionsPath, `${JSON.stringify(m)}\n`, { mode: 0o600 });
		} catch (e) {
			console.error(`usage · ${this.sessionsPath} could not be written: ${(e as Error).message}`);
		}
	}

	/** Everyone's, admins too: `resumes users usage` shows them all. */
	record(user: string | null, sid: string, usage: Usage | null | undefined, model: string): void {
		if (!user) return;
		const line: Line = { at: this.now(), user, sid, cost: Number(usage?.cost) || 0, tokens: (Number(usage?.in) || 0) + (Number(usage?.out) || 0), model };
		this.lines.push(line);
		try {
			appendFileSync(this.path, `${JSON.stringify(line)}\n`, { mode: 0o600 });
		} catch (e) {
			console.error(`usage · ${this.path} could not be written: ${(e as Error).message}`);
		}
	}
}
