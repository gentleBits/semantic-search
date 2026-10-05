/**
 * The chat's and the judge's provider, model and thinking level: the server's in `.resumes/settings.json` (with the
 * keys), each person's in `.resumes/user-settings.json`. The API never shows a key, not even masked, and never takes one.
 */
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { AuthStorage, type AuthStorageBackend } from "@earendil-works/pi-coding-agent";
import { ResumesError } from "./errors.js";
import { writeJsonAtomic } from "./files.js";
import { PROVIDERS, THINKING_LEVELS, provider as providerOf } from "./providers.js";
import { type Thinking, thinkingLevel } from "./text.js";

export const FILE = "settings.json";
export const USERS_FILE = "user-settings.json";
export const KEYS_ARE_THE_SERVERS = "keys are set on the server, not here: OPENAI_API_KEY / OPENROUTER_API_KEY in its environment, or the keys of .resumes/settings.json";

export interface Choice {
	provider?: string;
	model?: string;
	thinking?: string;
}

export interface Defaults {
	model: string;
	effort: string | null;
	judge_model: string | null;
	judge_effort: string | null;
}

export type Chosen = { provider: string; model: string; thinking: Thinking; from: "settings" | "config" | "chat" | "yours" };

export interface Picks {
	chat: Choice;
	judge: Choice;
}

interface FileShape {
	chat: Choice;
	judge: Choice;
	keys: Record<string, string>;
}

export function readFile(path: string): FileShape {
	let raw: unknown = null;
	try {
		raw = JSON.parse(readFileSync(path, "utf-8"));
	} catch {
		raw = null;
	}
	return readFileShape(raw);
}

function readFileShape(raw: unknown): FileShape {
	const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, any>;
	const pick = (o: unknown): Choice => {
		const c: Choice = {};
		if (o && typeof o === "object") {
			for (const k of ["provider", "model", "thinking"] as const) {
				const v = (o as Record<string, unknown>)[k];
				if (typeof v === "string" && v) c[k] = v;
			}
		}
		return c;
	};
	const keys: Record<string, string> = {};
	if (r.keys && typeof r.keys === "object") {
		for (const [k, v] of Object.entries(r.keys as Record<string, unknown>)) if (typeof v === "string" && v.trim()) keys[k] = v;
	}
	return { chat: pick(r.chat), judge: pick(r.judge), keys };
}

export function writeFile(path: string, data: FileShape): void {
	mkdirSync(dirname(path), { recursive: true });
	const tmp = `${path}.${process.pid}.${Date.now()}.tmp`;
	writeFileSync(tmp, JSON.stringify({ chat: data.chat, judge: data.judge, keys: data.keys }, null, 1), "utf-8");
	try {
		chmodSync(tmp, 0o600);
	} catch {
		// a file system without modes
	}
	renameSync(tmp, path);
}

/** pi's `AuthStorage` over the `keys` of settings.json: `{"openrouter": "sk-or-…"}` ⇄ `{"openrouter": {"type": "api_key", "key": "sk-or-…"}}`. */
export class SettingsKeysBackend implements AuthStorageBackend {
	constructor(public path: string) {}

	private load(): { file: FileShape; current: string } {
		const file = readFile(this.path);
		const auth: Record<string, { type: "api_key"; key: string }> = {};
		for (const [p, key] of Object.entries(file.keys)) auth[p] = { type: "api_key", key };
		return { file, current: JSON.stringify(auth) };
	}

	private store(file: FileShape, next: string): void {
		const keys: Record<string, string> = {};
		let parsed: unknown = {};
		try {
			parsed = JSON.parse(next);
		} catch {
			parsed = {};
		}
		if (parsed && typeof parsed === "object") {
			for (const [p, cred] of Object.entries(parsed as Record<string, any>)) {
				if (cred && cred.type === "api_key" && typeof cred.key === "string" && cred.key.trim()) keys[p] = cred.key;
			}
		}
		writeFile(this.path, { ...file, keys });
	}

	withLock<T>(fn: (current: string | undefined) => { result: T; next?: string }): T {
		const { file, current } = this.load();
		const { result, next } = fn(current);
		if (next !== undefined) this.store(file, next);
		return result;
	}

	async withLockAsync<T>(fn: (current: string | undefined) => Promise<{ result: T; next?: string }>): Promise<T> {
		const { file, current } = this.load();
		const { result, next } = await fn(current);
		if (next !== undefined) this.store(file, next);
		return result;
	}
}

export class Settings {
	chat: Choice = {};
	judge: Choice = {};
	readonly auth: AuthStorage;

	constructor(public path: string) {
		this.auth = AuthStorage.fromStorage(new SettingsKeysBackend(path));
		this.reload();
	}

	static in(stateDir: string): Settings {
		return new Settings(join(stateDir, FILE));
	}

	reload(): void {
		const f = readFile(this.path);
		this.chat = f.chat;
		this.judge = f.judge;
		this.auth.reload();
	}

	/** Writes the choices; the keys are kept as they are (they change only through `auth`). */
	save(): void {
		const f = existsSync(this.path) ? readFile(this.path) : { chat: {}, judge: {}, keys: {} };
		writeFile(this.path, { chat: this.chat, judge: this.judge, keys: f.keys });
	}

	/** The key for a provider: the settings' first, else the environment's (pi's own resolution). */
	key(provider: string): Promise<string | undefined> {
		return this.auth.getApiKey(provider, { includeFallback: false });
	}

	keySource(provider: string): "settings" | "env" | null {
		const s = this.auth.getAuthStatus(provider);
		if (s.configured && s.source === "stored") return "settings";
		return s.source === "environment" ? "env" : null;
	}

	chatChoice(d: Defaults, mine?: Picks | null): Chosen {
		if (mine?.chat.provider && mine.chat.model) {
			return { provider: mine.chat.provider, model: mine.chat.model, thinking: thinkingLevel(mine.chat.thinking || d.effort), from: "yours" };
		}
		if (this.chat.provider && this.chat.model) {
			return { provider: this.chat.provider, model: this.chat.model, thinking: thinkingLevel(this.chat.thinking || d.effort), from: "settings" };
		}
		const [provider, model] = parseModel(d.model);
		return { provider, model, thinking: thinkingLevel(this.chat.thinking || d.effort), from: "config" };
	}

	judgeChoice(d: Defaults, mine?: Picks | null): Chosen {
		if (mine?.chat.provider && mine.chat.model) {
			// a person who picked: their ranking model, or their chat model when they said "the same"
			const thinking = thinkingLevel(mine.judge.thinking || d.judge_effort);
			if (mine.judge.provider && mine.judge.model) return { provider: mine.judge.provider, model: mine.judge.model, thinking, from: "yours" };
			const c = this.chatChoice(d, mine);
			return { provider: c.provider, model: c.model, thinking, from: "chat" };
		}
		const thinking = thinkingLevel(this.judge.thinking || d.judge_effort);
		if (this.judge.provider && this.judge.model) return { provider: this.judge.provider, model: this.judge.model, thinking, from: "settings" };
		if (d.judge_model && !(this.chat.provider && this.chat.model)) {
			const [provider, model] = parseModel(d.judge_model);
			return { provider, model, thinking, from: "config" };
		}
		const c = this.chatChoice(d);
		return { provider: c.provider, model: c.model, thinking, from: "chat" };
	}

	async configured(): Promise<string[]> {
		const out: string[] = [];
		for (const p of Object.values(PROVIDERS)) if (await this.key(p.id)) out.push(p.id);
		return out;
	}

	async view(d: Defaults, mine?: Picks | null): Promise<Record<string, unknown>> {
		const c = this.chatChoice(d, mine);
		const j = this.judgeChoice(d, mine);
		const configured = await this.configured();
		return {
			chat: { provider: c.provider, model: c.model, thinking: c.thinking, from: c.from },
			judge: { provider: j.provider, model: j.model, thinking: j.thinking, from: j.from, same: j.from === "chat" },
			providers: Object.values(PROVIDERS).map((p) => ({ id: p.id, name: p.name, configured: configured.includes(p.id), public_list: p.public_list })),
			configured,
			thinking_levels: THINKING_LEVELS,
		};
	}

	apply(patch: unknown): void {
		const next = merged(patch, { chat: this.chat, judge: this.judge });
		this.chat = next.chat;
		this.judge = next.judge;
		this.save();
	}
}

/** A partial update over `base` → the new picks (checked; nothing saved). The keys are not the API's to change: a patch
 * that carries them is refused as a whole. */
export function merged(patch: unknown, base: Picks): Picks {
	if (!patch || typeof patch !== "object" || Array.isArray(patch)) throw new ResumesError("BAD_ARGUMENT", "the settings are an object");
	const p = patch as Record<string, unknown>;
	if ("keys" in p) throw new ResumesError("BAD_ARGUMENT", KEYS_ARE_THE_SERVERS);
	let chat = base.chat;
	let judge = base.judge;
	if ("chat" in p) chat = choiceOf(p.chat, "chat");
	if ("judge" in p) {
		const j = p.judge;
		judge = j === null || (j && typeof j === "object" && (j as Record<string, unknown>).same) ? {} : choiceOf(j, "judge");
	}
	return { chat, judge };
}

/** Each person's picks: `.resumes/user-settings.json`, mode 0600, read again when it changes. */
export class UserPicks {
	private seen = -1;
	private map = new Map<string, Picks>();

	constructor(public path: string) {}

	static in(stateDir: string): UserPicks {
		return new UserPicks(join(stateDir, USERS_FILE));
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
		for (const [name, v] of Object.entries((raw && typeof raw === "object" ? raw : {}) as Record<string, any>)) {
			const f = readFileShape(v);
			if (f.chat.provider && f.chat.model) this.map.set(name.trim().toLowerCase(), { chat: f.chat, judge: f.judge });
		}
	}

	get(name: string | null | undefined): Picks | null {
		if (!name) return null;
		this.load();
		return this.map.get(name.trim().toLowerCase()) ?? null;
	}

	set(name: string, picks: Picks): void {
		this.load();
		this.map.set(name.trim().toLowerCase(), picks);
		writeJsonAtomic(this.path, Object.fromEntries(this.map));
		this.seen = -1;
	}
}

export function parseModel(spec: string): [string, string] {
	const at = (spec || "").indexOf(":");
	const provider = at > 0 ? spec.slice(0, at).trim().toLowerCase() : "";
	const model = at > 0 ? spec.slice(at + 1).trim() : "";
	if (!provider || !model) {
		throw new ResumesError("BAD_CONFIG", `[web] model = ${JSON.stringify(spec)}: use provider:model, e.g. openai:gpt-5-mini or openrouter:anthropic/claude-sonnet-4.6`);
	}
	return [provider, model];
}

function choiceOf(value: unknown, what: string): Choice {
	if (!value || typeof value !== "object" || Array.isArray(value)) throw new ResumesError("BAD_ARGUMENT", `${what} is an object: {provider, model, thinking}`);
	const v = value as Record<string, unknown>;
	const out: Choice = {};
	if (v.provider || v.model) {
		const pid = providerOf(String(v.provider || "")).id;
		const model = String(v.model || "").trim();
		if (!model || (!model.includes("/") && pid === "openrouter")) {
			throw new ResumesError("BAD_ARGUMENT", pid === "openrouter" ? `the ${what} model on OpenRouter is vendor/model, e.g. openai/gpt-5-mini` : `the ${what} model is missing`);
		}
		out.provider = pid;
		out.model = model;
	}
	if (v.thinking !== undefined && v.thinking !== null) {
		const t = String(v.thinking).trim().toLowerCase();
		if (!THINKING_LEVELS.includes(t)) throw new ResumesError("BAD_ARGUMENT", `thinking is one of ${THINKING_LEVELS.join(", ")}`);
		out.thinking = t;
	}
	return out;
}
