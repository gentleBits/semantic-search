/**
 * Whose search a conversation is: `.resumes/owners.json`, {"<sid>": {user, since}}. The engine knows nothing of owners;
 * a conversation with no owner is nobody's while login is on.
 */
import { mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

export const OWNERS_FILE = "owners.json";

export class Owners {
	private seen = -1;
	private map = new Map<string, { user: string; since: string }>();

	constructor(public path: string) {}

	static in(stateDir: string): Owners {
		return new Owners(join(stateDir, OWNERS_FILE));
	}

	private load(): void {
		let stamp = 0;
		try {
			const s = statSync(this.path);
			stamp = s.mtimeMs + s.size / 1e9;
		} catch {
			stamp = 0;
		}
		if (stamp === this.seen) return;
		this.seen = stamp;
		this.map = new Map();
		if (!stamp) return;
		let raw: any = null;
		try {
			raw = JSON.parse(readFileSync(this.path, "utf-8"));
		} catch {
			raw = null;
		}
		if (raw && typeof raw === "object") {
			for (const [sid, rec] of Object.entries(raw as Record<string, any>)) {
				if (rec && typeof rec.user === "string" && rec.user) this.map.set(sid, { user: rec.user, since: typeof rec.since === "string" ? rec.since : "" });
			}
		}
	}

	private save(): void {
		mkdirSync(dirname(this.path), { recursive: true });
		const tmp = `${this.path}.${process.pid}.${Date.now()}.tmp`;
		writeFileSync(tmp, JSON.stringify(Object.fromEntries(this.map), null, 1), "utf-8");
		renameSync(tmp, this.path);
		this.seen = -1; // re-read next time: the stamp is the file's, not ours
	}

	of(sid: string): string | null {
		this.load();
		return this.map.get(sid)?.user ?? null;
	}

	set(sid: string, user: string): void {
		this.load();
		this.map.set(sid, { user, since: new Date().toISOString() });
		this.save();
	}

	forget(sid: string): void {
		this.load();
		if (!this.map.delete(sid)) return;
		this.save();
	}

	mine(user: string): Set<string> {
		this.load();
		const out = new Set<string>();
		for (const [sid, rec] of this.map) if (rec.user === user) out.add(sid);
		return out;
	}
}
