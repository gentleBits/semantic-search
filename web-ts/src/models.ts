/**
 * Which model, with which key, through pi in-process: one pi-ai `Models` collection holding the OpenAI and OpenRouter
 * providers (their catalogs, their streams), the keys of settings.json as its credential store, OpenRouter's live list,
 * and pi-ai's scripted `faux` provider for the tests (`RESUMES_FAUX=1`).
 */
import { type Api, type AssistantMessageEventStream, type Context, type FauxProviderHandle, type Model, type MutableModels, type SimpleStreamOptions, clampThinkingLevel, createAssistantMessageEventStream, createModels, fauxProvider } from "@earendil-works/pi-ai";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import { openrouterProvider } from "@earendil-works/pi-ai/providers/openrouter";
import { ResumesError } from "./errors.js";
import { type Catalog, PROVIDERS } from "./providers.js";
import type { Chosen, Settings } from "./settings.js";
import type { Thinking } from "./text.js";

/** Tries a call makes when the provider answers 429 or 5xx (pi-ai leaves it to the caller: 0 unless asked). */
export const RETRIES = 2;

/** Silence after which a model call is given up. pi's timeout and retries cover only the wait for the first byte: a
 * reply that stalls halfway would wait for ever (a ranking batch did, 13 minutes, until Stop). */
export const IDLE_MS = 180_000;

/** Every model call: through pi's collection, with the retries; nothing for `idleMs` ends it as an error (not as a
 * stop: the caller's own signal still means "stopped"). */
export type Stream = (model: Model<Api>, context: Context, options?: SimpleStreamOptions) => AssistantMessageEventStream;

export function guardedStream(pi: MutableModels, idleMs = IDLE_MS): Stream {
	return (model, context, options) => {
		const ac = new AbortController();
		const outer = options?.signal;
		const stop = () => ac.abort(outer?.reason);
		if (outer?.aborted) ac.abort(outer.reason);
		else outer?.addEventListener("abort", stop, { once: true });
		let idled = false;
		let timer: ReturnType<typeof setTimeout> | undefined;
		const touch = () => {
			clearTimeout(timer);
			timer = setTimeout(() => {
				idled = true;
				ac.abort();
			}, idleMs);
		};
		touch();
		const inner = pi.streamSimple(model, context, { maxRetries: RETRIES, ...options, signal: ac.signal });
		const out = createAssistantMessageEventStream();
		void (async () => {
			try {
				for await (const e of inner) {
					touch();
					if (idled && !outer?.aborted && e.type === "error") {
						out.push({ type: "error", reason: "error", error: { ...e.error, stopReason: "error", errorMessage: `no reply from the model for ${Math.round(idleMs / 1000)} s` } });
					} else out.push(e);
				}
			} finally {
				clearTimeout(timer);
				outer?.removeEventListener("abort", stop);
				out.end();
			}
		})();
		return out;
	};
}

export interface ModelChoice {
	provider: string;
	id: string;
	name: string; // provider:id
	model: Model<Api>;
	key: string | undefined;
	thinking: Thinking;
	from: Chosen["from"];
	pi: MutableModels; // the collection that streams it
	stream: Stream; // its calls: the retries, the silence limit
}

export class Models {
	readonly pi: MutableModels;
	faux: FauxProviderHandle | null = null;

	constructor(public settings: Settings) {
		this.pi = createModels({ credentials: settings.credentials });
		this.pi.setProvider(openaiProvider());
		this.pi.setProvider(openrouterProvider());
	}

	/** Every model pi's catalogs know, across the providers. */
	all(): Model<Api>[] {
		return [...this.pi.getModels()];
	}

	/** pi-ai's faux provider: `faux:faux` and `faux:faux-thinker`, answering what `faux.setResponses()` scripts. */
	enableFaux(tokensPerSecond = 2000): FauxProviderHandle {
		if (!this.faux) {
			this.faux = fauxProvider({ provider: "faux", models: [{ id: "faux" }, { id: "faux-thinker", reasoning: true }], tokensPerSecond });
			this.pi.setProvider(this.faux.provider);
		}
		return this.faux;
	}

	needsKey(provider: string): boolean {
		return provider in PROVIDERS;
	}

	async resolve(provider: string, id: string, catalog: Catalog): Promise<Model<Api>> {
		if (provider === "faux") {
			const m = this.faux?.getModel(id);
			if (!m) throw new ResumesError("ASSISTANT_UNAVAILABLE", this.faux ? `unknown faux model ${id}` : "faux models are for the tests (RESUMES_FAUX=1)");
			return m as Model<Api>;
		}
		if (provider === "openrouter") return catalog.openrouter(id);
		const m = this.pi.getModel(provider, id);
		if (m) return m;
		if (provider === "openai") {
			const custom = await catalog.openai(id); // in OpenAI's own list: called as a custom id, as the terminal pi does
			if (custom) return custom;
		}
		throw new ResumesError("ASSISTANT_UNAVAILABLE", `unknown model ${provider}/${id}`);
	}

	async choice(c: Chosen, catalog: Catalog): Promise<ModelChoice> {
		const model = await this.resolve(c.provider, c.model, catalog);
		const key = await this.settings.key(c.provider);
		// a level the model does not take is the one it runs at (Sol and Astra: off and minimal are low), said, not left to OpenAI's default
		const thinking = model.reasoning ? (clampThinkingLevel(model, c.thinking) as Thinking) : c.thinking;
		return { provider: c.provider, id: c.model, name: `${c.provider}:${c.model}`, model, key, thinking, from: c.from, pi: this.pi, stream: guardedStream(this.pi) };
	}

	async whyNot(c: Chosen, catalog?: Catalog): Promise<string | null> {
		if (c.provider === "faux" && !this.faux) return "faux models are for the tests (RESUMES_FAUX=1)";
		if (this.needsKey(c.provider) && !(await this.settings.key(c.provider))) {
			return `no key for ${c.provider} on this server (${PROVIDERS[c.provider].env})`;
		}
		if (c.provider !== "openrouter" && c.provider !== "faux" && !this.pi.getModel(c.provider, c.model)) {
			if (!(c.provider === "openai" && catalog && (await catalog.row("openai", c.model)))) return `unknown model ${c.provider}/${c.model}`;
		}
		return null;
	}
}
