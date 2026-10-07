/**
 * The chat's providers and the models each offers: on OpenRouter a few free ones that take tools (the operator pays
 * for nothing; `pickFree`), on OpenAI the GPT-6 models of OPENAI_MODELS. Lists are cached an hour, in memory and on disk.
 */
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { type Api, type Model, getSupportedThinkingLevels } from "@earendil-works/pi-ai";
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

/** The OpenAI models on offer, most capable first: Astra (the frontier one), Sol (everyday), Luna (fast and cheap). */
export const OPENAI_MODELS = ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"];

export function provider(pid: string): Provider {
	const p = PROVIDERS[(pid || "").toLowerCase()];
	if (!p) throw new ResumesError("BAD_ARGUMENT", `no provider “${pid}”: use one of ${Object.keys(PROVIDERS).join(", ")}`);
	return p;
}

export type Fetch = (url: string, headers?: Record<string, string>, body?: unknown) => Promise<{ status: number; body: any; text?: string }>;

/** → {status, JSON body, the text}; a `body` makes it a JSON POST. Network trouble is a ResumesError; an HTTP error status
 * is returned, not thrown. */
export const httpGet: Fetch = async (url, headers = {}, payload) => {
	let res: Response;
	try {
		const post = payload === undefined ? {} : { method: "POST", body: JSON.stringify(payload) };
		res = await fetch(url, { ...post, headers: { Accept: "application/json", "User-Agent": "resumes-web", ...(payload === undefined ? {} : { "Content-Type": "application/json" }), ...headers }, signal: AbortSignal.timeout(TIMEOUT_MS) });
	} catch (e) {
		const reason = (e as Error)?.name === "TimeoutError" ? "timed out" : String((e as any)?.cause?.message || (e as Error)?.message || e);
		throw new ResumesError("PROVIDER_UNREACHABLE", `${new URL(url).host}: ${reason}`);
	}
	let text = "";
	try {
		text = await res.text();
	} catch {
		text = "";
	}
	let body: any = {};
	try {
		body = JSON.parse(text);
	} catch {
		body = {};
	}
	return { status: res.status, body: body && typeof body === "object" ? body : {}, text };
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
	levels?: string[]; // the thinking levels it takes, of THINKING_LEVELS (Sol and Astra: no off, no minimal)
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

export const OPENROUTER_RANKINGS = "https://openrouter.ai/rankings";

/** The labs whose free models OpenRouter's list offers, besides the free versions of the week's top 10. */
export const OPENROUTER_MAKERS = ["nvidia", "google", "meta-llama", "mistralai", "qwen", "deepseek", "openai", "anthropic", "microsoft", "x-ai", "z-ai", "moonshotai", "xiaomi", "thinkingmachines", "poolside", "cohere", "amazon", "tencent", "minimax"];
export const OPENROUTER_MAX = 8;
export const PROBE_TTL_MS = 7 * 24 * 3600_000; // a model's answer to the one-word try is kept a week
const SMALL = /(^|[-.])(nano|mini|small|xs|tiny|lite|micro|lightning)([-.]|$)/;

/** The week's top models on OpenRouter, best first: the leaderboard its rankings page publishes (schema.org
 * "Top LLMs by weekly token usage"). There is no API for it: a page that has changed gives []. */
export function topIds(html: string): string[] {
	const t = html.replace(/\\"/g, '"');
	const at = t.indexOf("Top LLMs by weekly token usage");
	if (at < 0) return [];
	return [...t.slice(at, at + 8000).matchAll(/"position":\d+,"name":"[^"]*","item":"https:\/\/openrouter\.ai\/([a-z0-9._-]+\/[a-z0-9._:-]+)"/g)].map((m) => m[1]);
}

/** Billions of parameters a model's id says it has: "nemotron-3-ultra-550b-a55b" → 550 in all, 55 at a time. */
export function sizeOf(id: string): { total: number | null; active: number | null } {
	const name = (id.split("/")[1] || id).replace(/:free$/, "");
	const total = /(?:^|-)(\d+(?:\.\d+)?)b(?=$|-)/.exec(name);
	const active = /-a(\d+(?:\.\d+)?)b(?=$|-)/.exec(name);
	return { total: total ? Number(total[1]) : null, active: active ? Number(active[1]) : null };
}

/** The free models worth offering, at most OPENROUTER_MAX: the free versions of the week's top models first (in their
 * order), then the free models of OPENROUTER_MAKERS that are not small (no nano/mini/small/xs…, at least 20B in all and
 * 10B at a time when the id says), not a preview; last, OpenRouter's own free router (a free model at random). Never
 * an anonymous "stealth" model. */
export function pickFree(rows: Row[], top: string[] = []): Row[] {
	const router = rows.find((r) => r.id === "openrouter/free" && isFree(r));
	const free = rows.filter(isFree).filter((r) => !/^(openrouter|stealth)\//.test(r.id));
	const base = (id: string) => id.replace(/:free$/, "");
	const ranked: Row[] = [];
	for (const t of top) for (const r of free) if (base(r.id) === base(t) && !ranked.includes(r)) ranked.push(r);
	const sound = (r: Row) => {
		const name = base(r.id.split("/")[1] || r.id);
		const { total, active } = sizeOf(r.id);
		return OPENROUTER_MAKERS.includes(r.id.split("/")[0]) && !SMALL.test(name) && !/preview/.test(name) && (total === null || total >= 20) && (active === null || active >= 10);
	};
	const rest = free.filter((r) => !ranked.includes(r) && sound(r)).sort(byName);
	return [...[...ranked, ...rest].slice(0, OPENROUTER_MAX - (router ? 1 : 0)), ...(router ? [router] : [])];
}

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
			...(params.includes("reasoning") || params.includes("reasoning_effort") ? { levels: THINKING_LEVELS } : {}),
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

/** The models of `allowed`, in that order: pi's registry rows, plus a live id it does not know (no price or context). */
export function openaiRows(registry: Model<Api>[], live: string[] | null, allowed: string[] = OPENAI_MODELS): Row[] {
	const rows = registryRows(registry.filter((m) => m.provider === "openai")).filter((r) => openaiOffered(r.id));
	const known = new Set(rows.map((r) => r.id));
	for (const id of live || []) {
		if (known.has(id) || !openaiOffered(id)) continue;
		known.add(id);
		const reasoning = /^(gpt-[5-9]|o\d)/.test(id);
		rows.push({ id, name: id, context: 0, max_tokens: FALLBACK_MAX_TOKENS, in_per_m: null, out_per_m: null, cache_read_per_m: null, reasoning, ...(reasoning ? { levels: THINKING_LEVELS } : {}), image: false, structured: true });
	}
	return offered(rows, allowed);
}

const offered = (rows: Row[], allowed: string[]): Row[] => allowed.map((id) => rows.find((r) => r.id === id)).filter((r): r is Row => !!r);

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
			...(m.reasoning ? { levels: THINKING_LEVELS.filter((l) => getSupportedThinkingLevels(m).includes(l as any)) } : {}),
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
	private pending = new Map<string, Promise<unknown>>(); // provider → its list being fetched in the background
	openaiModels: string[] = OPENAI_MODELS; // the OpenAI models on offer
	blocking = false; // the tests: a list that is due is fetched before the answer

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

	/** What ships with the app, for a first start with nothing cached: OpenAI's from pi's registry, OpenRouter's from
	 * data/openrouter-free.json (the picks when it was written). The live list replaces it seconds later. */
	private shipped(pid: string): Entry | null {
		if (pid === "openai") return { provider: pid, rows: openaiRows(this.registryModels(), null, this.openaiModels), fetched_at: null, at: 0, source: "pi registry" };
		try {
			const d = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "data", "openrouter-free.json"), "utf-8"));
			return Array.isArray(d.rows) ? { provider: pid, rows: d.rows, fetched_at: d.fetched_at || null, at: 0, source: "shipped with the app" } : null;
		} catch {
			return null;
		}
	}

	/** A provider's models, at once: the list in memory, else on disk, else the one shipped with the app, whatever its
	 * age (an old list now beats a fresh one waited for). A list over an hour old, or `refresh`, is fetched again in the
	 * background for the next time. */
	async list(pid: string, opts: { refresh?: boolean } = {}): Promise<Listing> {
		const p = provider(pid);
		if (this.blocking) return this.fetchList(p.id, !!opts.refresh);
		let entry = this.memory.get(p.id);
		if (!entry) {
			entry = this.readDisk(p.id) || this.shipped(p.id) || undefined;
			if (entry) this.memory.set(p.id, entry);
		}
		if (opts.refresh || !entry || Date.now() - entry.at >= TTL_MS) this.inBackground(p.id, !!opts.refresh);
		return entry ? view(entry) : { models: [], fetched_at: null, source: null, note: "the list is on its way" };
	}

	private inBackground(pid: string, refresh: boolean): void {
		if (this.pending.has(pid)) return;
		const job = this.fetchList(pid, refresh)
			.catch(() => undefined)
			.finally(() => this.pending.delete(pid));
		this.pending.set(pid, job);
	}

	/** The lists being fetched in the background, done (the tests). */
	async settled(): Promise<void> {
		await Promise.all([...this.pending.values()]);
	}

	/** Fetches a provider's list (the top models, the one-word tries): seconds, so only ever in the background — or for
	 * the tests (`blocking`). */
	private async fetchList(pid: string, refresh: boolean): Promise<Listing> {
		const p = provider(pid);
		let entry = this.memory.get(p.id);
		if (entry && !refresh && Date.now() - entry.at < TTL_MS) return view(entry);
		const opts = { refresh };
		let note: string | undefined;
		try {
			if (!opts.refresh && Date.now() - (this.failed.get(p.id) || 0) < RETRY_AFTER_MS) {
				throw new ResumesError("PROVIDER_UNREACHABLE", `${p.name} could not be reached a moment ago`);
			}
			if (p.id === "openrouter") {
				const { status, body } = await this.fetch(`${OPENROUTER_URL}/models`);
				if (status !== 200) throw new ResumesError("PROVIDER_UNREACHABLE", `openrouter.ai answered HTTP ${status}`);
				const top = await this.openrouterTop();
				let rows = openrouterRows(body);
				let picked = pickFree(rows, top);
				// a free model OpenRouter lists may still refuse this server (some are for approved apps only): each pick is
				// tried once with one word, and one that refuses gives its place to the next
				const tried = new Set<string>();
				for (let round = 0; round < 3; round++) {
					const refused = await this.refusedOf(picked.map((r) => r.id), tried);
					if (!refused.size) break;
					rows = rows.filter((r) => !refused.has(r.id));
					picked = pickFree(rows, top);
				}
				entry = this.keep(p.id, picked, top.length ? "live · free models, this week's top first" : "live · free models");
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
				entry = this.keep(p.id, openaiRows(this.registryModels(), live, this.openaiModels), live ? "pi registry + OpenAI's list" : "pi registry");
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

	private probes: Record<string, { status: number; at: number }> | null = null;

	private probesFile(): string | null {
		return this.cacheDir ? join(this.cacheDir, "openrouter-tries.json") : null;
	}

	/** Of these OpenRouter models, those that refused a one-word try (403: for approved apps only; 404: gone), each tried
	 * at most once a week with the server's key. A busy model (429) or no answer counts as fine and is tried again later. */
	async refusedOf(ids: string[], tried = new Set<string>()): Promise<Set<string>> {
		if (!this.probes) {
			const f = this.probesFile();
			try {
				this.probes = f ? JSON.parse(readFileSync(f, "utf-8")) : {};
			} catch {
				this.probes = {};
			}
		}
		const probes = this.probes!;
		const key = await this.keyFor("openrouter");
		const due = ids.filter((id) => !tried.has(id) && (!probes[id] || Date.now() - probes[id].at > PROBE_TTL_MS));
		for (const id of due) tried.add(id); // once per refresh
		if (key && due.length) {
			await Promise.all(
				due.map(async (id) => {
					try {
						const { status } = await this.fetch(`${OPENROUTER_URL}/chat/completions`, { Authorization: `Bearer ${key}` }, { model: id, messages: [{ role: "user", content: "hi" }], max_tokens: 1 });
						if (status !== 429 && status !== 401 && status < 500) probes[id] = { status, at: Date.now() };
					} catch {
						// no answer: tried again at the next refresh
					}
				}),
			);
			const f = this.probesFile();
			if (f) {
				try {
					mkdirSync(this.cacheDir!, { recursive: true });
					writeFileSync(f, JSON.stringify(probes), "utf-8");
				} catch {
					// the record is a convenience
				}
			}
		}
		return new Set(ids.filter((id) => probes[id] && [403, 404].includes(probes[id].status) && Date.now() - probes[id].at <= PROBE_TTL_MS));
	}

	/** The week's top models (OpenRouter's rankings page); any trouble: none. */
	private async openrouterTop(): Promise<string[]> {
		try {
			const { status, text } = await this.fetch(OPENROUTER_RANKINGS);
			return status === 200 && text ? topIds(text) : [];
		} catch {
			return [];
		}
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
