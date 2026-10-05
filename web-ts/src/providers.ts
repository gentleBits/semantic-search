/**
 * The chat's providers and the models each offers: on OpenRouter only the free ones that take tools (the operator pays
 * for nothing), on OpenAI pi's registry plus OpenAI's own list. Lists are cached an hour, in memory and on disk.
 */
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { Api, Model } from "@earendil-works/pi-ai";
import { ResumesError } from "./errors.js";

export const OPENROUTER_URL = "https://openrouter.ai/api/v1";
export const OPENAI_URL = "https://api.openai.com/v1";
export const TTL_MS = 3600_000;
export const RETRY_AFTER_MS = 60_000;
export const TIMEOUT_MS = 15_000;
export const FALLBACK_MAX_TOKENS = 16384;

export interface Provider {
	id: string;
	name: string;
	env: string;
	public_list: boolean; // the model list needs no key
}

export const PROVIDERS: Record<string, Provider> = {
	openrouter: { id: "openrouter", name: "OpenRouter", env: "OPENROUTER_API_KEY", public_list: true },
	openai: { id: "openai", name: "OpenAI", env: "OPENAI_API_KEY", public_list: false },
};
export const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high"];

export function provider(pid: string): Provider {
	const p = PROVIDERS[(pid || "").toLowerCase()];
	if (!p) throw new ResumesError("BAD_ARGUMENT", `no provider “${pid}”: use one of ${Object.keys(PROVIDERS).join(", ")}`);
	return p;
}

export type Fetch = (url: string, headers?: Record<string, string>) => Promise<{ status: number; body: any }>;

/** → {status, JSON body}. Network trouble is a ResumesError; an HTTP error status is returned, not thrown. */
export const httpGet: Fetch = async (url, headers = {}) => {
	let res: Response;
	try {
		res = await fetch(url, { headers: { Accept: "application/json", "User-Agent": "resumes-web", ...headers }, signal: AbortSignal.timeout(TIMEOUT_MS) });
	} catch (e) {
		const reason = (e as Error)?.name === "TimeoutError" ? "timed out" : String((e as any)?.cause?.message || (e as Error)?.message || e);
		throw new ResumesError("PROVIDER_UNREACHABLE", `${new URL(url).host}: ${reason}`);
	}
	let body: any = {};
	try {
		body = await res.json();
	} catch {
		body = {};
	}
	return { status: res.status, body: body && typeof body === "object" ? body : {} };
};

export interface Row {
	id: string;
	name: string;
	context: number;
	max_tokens: number;
	in_per_m: number | null; // null: no price known
	out_per_m: number | null;
	cache_read_per_m: number | null;
	reasoning: boolean;
	image: boolean;
	structured: boolean;
}

/** OpenRouter prices are USD per token, as strings. */
export function perMillion(price: unknown): number {
	const n = Number.parseFloat(String(price));
	return Number.isFinite(n) ? Math.round(n * 1_000_000 * 10000) / 10000 : 0;
}

const round4 = (x: unknown): number => Math.round((Number(x) || 0) * 10000) / 10000;
const byName = (a: Row, b: Row) => (a.name.toLowerCase() < b.name.toLowerCase() ? -1 : a.name.toLowerCase() > b.name.toLowerCase() ? 1 : 0);

export const isFree = (r: Row): boolean => r.in_per_m === 0 && r.out_per_m === 0;

export function openrouterRows(raw: any): Row[] {
	const rows: Row[] = [];
	for (const m of raw?.data || []) {
		const params: string[] = m.supported_parameters || [];
		if (!params.includes("tools") || !m.id) continue;
		const pricing = m.pricing || {};
		const top = m.top_provider || {};
		const arch = m.architecture || {};
		rows.push({
			id: m.id,
			name: m.name || m.id,
			context: Number(m.context_length || top.context_length || 0),
			max_tokens: Number(top.max_completion_tokens || 0) || FALLBACK_MAX_TOKENS,
			in_per_m: perMillion(pricing.prompt),
			out_per_m: perMillion(pricing.completion),
			cache_read_per_m: perMillion(pricing.input_cache_read),
			reasoning: params.includes("reasoning") || params.includes("reasoning_effort"),
			image: (arch.input_modalities || []).includes("image"),
			structured: params.includes("structured_outputs") || params.includes("response_format"),
		});
	}
	return rows.sort(byName);
}

/** Keep the chat models (the assistant needs tools and text), not speech, images, embeddings, search. */
const NOT_A_CHAT_MODEL = /realtime|audio|transcribe|tts|image|search|instruct|embedding|moderation|diarize|dall-e|whisper|sora|computer-use|codex-spark/;
export const chatModelId = (id: string): boolean => /^(gpt-|o\d)/.test(id) && !NOT_A_CHAT_MODEL.test(id);

/** An OpenAI id's generation (the o-series counts as 4.x), and whether it is a dated snapshot (gpt-4-0613) or an alias
 * (gpt-5.2-chat-latest). */
export function openaiVersion(id: string): { generation: number; snapshot: boolean; alias: boolean } | null {
	const g = /^gpt-(\d+(?:\.\d+)?)/.exec(id);
	const o = /^o(\d+)/.exec(id);
	if (!g && !o) return null;
	return {
		generation: g ? Number(g[1]) : 4 + Number(o![1]) / 10,
		snapshot: /-\d{4}-\d{2}-\d{2}$|-\d{4}$|-16k$/.test(id),
		alias: /-chat-latest$|-latest$|deep-research|^gpt-live/.test(id),
	};
}

/** One row per model version: no dated snapshots, no aliases, nothing older than GPT-4. */
export function openaiOffered(id: string): boolean {
	const v = openaiVersion(id);
	return !!v && chatModelId(id) && !v.snapshot && !v.alias && v.generation >= 4;
}

const byVersion = (a: Row, b: Row) => {
	const ga = openaiVersion(a.id)?.generation || 0;
	const gb = openaiVersion(b.id)?.generation || 0;
	return gb - ga || a.id.length - b.id.length || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
};

/** pi's registry rows plus the live ids it does not know (no price or context known), newest first. */
export function openaiRows(registry: Model<Api>[], live: string[] | null): Row[] {
	const rows = registryRows(registry.filter((m) => m.provider === "openai")).filter((r) => openaiOffered(r.id));
	const known = new Set(rows.map((r) => r.id));
	for (const id of live || []) {
		if (known.has(id) || !openaiOffered(id)) continue;
		known.add(id);
		rows.push({ id, name: id, context: 0, max_tokens: FALLBACK_MAX_TOKENS, in_per_m: null, out_per_m: null, cache_read_per_m: null, reasoning: /^(gpt-[5-9]|o\d)/.test(id), image: false, structured: true });
	}
	return rows.sort(byVersion);
}

/** An OpenAI id pi's registry does not know, called as the terminal pi does: the provider's default model with the id
 * put in; the price is not known, so it counts as 0. */
export function openaiModel(id: string, registry: Model<Api>[]): Model<"openai-responses"> & { unlisted: true } {
	const openai = registry.filter((m) => m.provider === "openai");
	const base = (openai.find((m) => m.id === "gpt-5-mini") || openai[0]) as Model<"openai-responses"> | undefined;
	const model: Model<"openai-responses"> = base
		? { ...base }
		: { id, name: id, api: "openai-responses", provider: "openai", baseUrl: OPENAI_URL, reasoning: true, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 400000, maxTokens: 128000 };
	return { ...model, id, name: id, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, unlisted: true };
}

export function registryRows(models: Model<Api>[]): Row[] {
	return models
		.map((m) => ({
			id: m.id,
			name: m.name || m.id,
			context: Number(m.contextWindow || 0),
			max_tokens: Number(m.maxTokens || 0) || FALLBACK_MAX_TOKENS,
			in_per_m: round4(m.cost?.input),
			out_per_m: round4(m.cost?.output),
			cache_read_per_m: round4(m.cost?.cacheRead),
			reasoning: !!m.reasoning,
			image: (m.input || []).includes("image"),
			structured: true,
		}))
		.sort(byName);
}

/** A picker row → the pi-ai Model OpenRouter is called with (compat is auto-detected from the URL). */
export function openrouterModel(row: Partial<Row> & { id: string }): Model<"openai-completions"> {
	return {
		id: row.id,
		name: row.name || row.id,
		api: "openai-completions",
		provider: "openrouter",
		baseUrl: OPENROUTER_URL,
		reasoning: !!row.reasoning,
		input: row.image ? ["text", "image"] : ["text"],
		cost: { input: row.in_per_m || 0, output: row.out_per_m || 0, cacheRead: row.cache_read_per_m || 0, cacheWrite: 0 },
		contextWindow: row.context || 0,
		maxTokens: row.max_tokens || FALLBACK_MAX_TOKENS,
	};
}

/** A model id typed by hand that the list does not know: called anyway, with no price known. */
export function unlistedModel(mid: string): Model<"openai-completions"> & { unlisted: true } {
	return { ...openrouterModel({ id: mid, name: mid, context: 128000, max_tokens: FALLBACK_MAX_TOKENS, reasoning: false }), unlisted: true };
}

interface Entry {
	provider: string;
	rows: Row[];
	fetched_at: string | null;
	at: number;
	source: string;
}

export interface Listing {
	models: Row[];
	fetched_at: string | null;
	source: string | null;
	note?: string;
}

export class Catalog {
	private memory = new Map<string, Entry>();
	private failed = new Map<string, number>(); // provider → when the last live fetch failed (not tried again for a minute)

	constructor(
		public cacheDir: string | null,
		public fetch: Fetch = httpGet,
		public registryModels: () => Model<Api>[] = () => [],
		public keyFor: (pid: string) => Promise<string | undefined> = async () => undefined,
	) {}

	private disk(pid: string): string | null {
		return this.cacheDir ? join(this.cacheDir, `${pid}-models.json`) : null;
	}

	private readDisk(pid: string): Entry | null {
		const p = this.disk(pid);
		if (!p) return null;
		try {
			const d = JSON.parse(readFileSync(p, "utf-8"));
			return d && Array.isArray(d.rows) ? { ...d, at: 0 } : null;
		} catch {
			return null;
		}
	}

	private keep(pid: string, rows: Row[], source: string): Entry {
		const entry: Entry = { provider: pid, rows, fetched_at: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"), at: Date.now(), source };
		this.memory.set(pid, entry);
		const p = this.disk(pid);
		if (p) {
			try {
				mkdirSync(this.cacheDir!, { recursive: true });
				const tmp = `${p}.${process.pid}.tmp`;
				writeFileSync(tmp, JSON.stringify({ provider: pid, rows, fetched_at: entry.fetched_at, source }), "utf-8");
				renameSync(tmp, p);
			} catch {
				// the cache is a convenience
			}
		}
		return entry;
	}

	async list(pid: string, opts: { refresh?: boolean } = {}): Promise<Listing> {
		const p = provider(pid);
		let entry = this.memory.get(p.id);
		if (entry && !opts.refresh && Date.now() - entry.at < TTL_MS) return view(entry);
		let note: string | undefined;
		try {
			if (!opts.refresh && Date.now() - (this.failed.get(p.id) || 0) < RETRY_AFTER_MS) {
				throw new ResumesError("PROVIDER_UNREACHABLE", `${p.name} could not be reached a moment ago`);
			}
			if (p.id === "openrouter") {
				const { status, body } = await this.fetch(`${OPENROUTER_URL}/models`);
				if (status !== 200) throw new ResumesError("PROVIDER_UNREACHABLE", `openrouter.ai answered HTTP ${status}`);
				entry = this.keep(p.id, openrouterRows(body).filter(isFree), "live · free models");
			} else if (p.id === "openai") {
				let live: string[] | null = null;
				const key = await this.keyFor("openai");
				if (key) {
					try {
						const { status, body } = await this.fetch(`${OPENAI_URL}/models`, { Authorization: `Bearer ${key}` });
						if (status === 200) live = (body?.data || []).map((m: any) => String(m.id || "")).filter(Boolean);
						else note = `api.openai.com answered HTTP ${status}: the list is pi's registry alone`;
					} catch (e) {
						note = `${String((e as Error)?.message || e)}: the list is pi's registry alone`;
					}
				}
				entry = this.keep(p.id, openaiRows(this.registryModels(), live), live ? "pi registry + OpenAI's list" : "pi registry");
			}
		} catch (e) {
			if (!(e instanceof ResumesError)) throw e;
			note = e.message;
			if (!note.includes("a moment ago")) this.failed.set(p.id, Date.now());
			entry = entry || this.readDisk(p.id) || undefined;
			if (!entry) return { models: [], fetched_at: null, source: null, note };
			entry = { ...entry, source: `${entry.source || "cache"} (offline)` };
			this.memory.set(p.id, entry);
		}
		if (p.id === "openrouter" && !entry!.rows.every(isFree)) entry = { ...entry!, rows: entry!.rows.filter(isFree) }; // an older cached list may hold paid rows
		return view(entry!, note);
	}

	async row(pid: string, mid: string): Promise<Row | null> {
		const rows = (await this.list(pid)).models;
		return rows.find((r) => r.id === mid) || null;
	}

	async openrouter(mid: string): Promise<Model<"openai-completions">> {
		const row = await this.row("openrouter", mid);
		return row ? openrouterModel(row) : unlistedModel(mid);
	}

	async openai(mid: string): Promise<(Model<"openai-responses"> & { unlisted: true }) | null> {
		const row = await this.row("openai", mid);
		return row ? openaiModel(mid, this.registryModels()) : null;
	}
}

function view(entry: Entry, note?: string): Listing {
	return { models: entry.rows, fetched_at: entry.fetched_at, source: entry.source, ...(note ? { note } : {}) };
}
