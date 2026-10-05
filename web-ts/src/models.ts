/**
 * Which model, with which key, through pi in-process: pi's registry and AuthStorage, OpenRouter's live list, and pi-ai's
 * scripted `faux` provider for the tests (`RESUMES_FAUX=1`).
 */
import type { Api, FauxProviderRegistration, Model } from "@earendil-works/pi-ai";
import { registerFauxProvider } from "@earendil-works/pi-ai";
import { ModelRegistry } from "@earendil-works/pi-coding-agent";
import { ResumesError } from "./errors.js";
import { type Catalog, PROVIDERS } from "./providers.js";
import type { Chosen, Settings } from "./settings.js";
import type { Thinking } from "./text.js";

export interface ModelChoice {
	provider: string;
	id: string;
	name: string; // provider:id
	model: Model<Api>;
	key: string | undefined;
	thinking: Thinking;
	from: Chosen["from"];
}

export class Models {
	readonly registry: ModelRegistry;
	faux: FauxProviderRegistration | null = null;

	constructor(public settings: Settings) {
		this.registry = ModelRegistry.inMemory(settings.auth);
	}

	/** pi-ai's faux provider: `faux:faux` and `faux:faux-thinker`, answering what `faux.setResponses()` scripts. */
	enableFaux(tokensPerSecond = 2000): FauxProviderRegistration {
		if (!this.faux) {
			this.faux = registerFauxProvider({ provider: "faux", models: [{ id: "faux" }, { id: "faux-thinker", reasoning: true }], tokensPerSecond });
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
			return m;
		}
		if (provider === "openrouter") return catalog.openrouter(id);
		const m = this.registry.find(provider, id);
		if (m) return m;
		if (provider === "openai") {
			const custom = await catalog.openai(id); // in OpenAI's own list: called as a custom id, as the terminal pi does
			if (custom) return custom;
		}
		throw new ResumesError("ASSISTANT_UNAVAILABLE", `unknown model ${provider}/${id}`);
	}

	async choice(c: Chosen, catalog: Catalog): Promise<ModelChoice> {
		const model = await this.resolve(c.provider, c.model, catalog);
		const key = await this.registry.getApiKeyForProvider(c.provider);
		return { provider: c.provider, id: c.model, name: `${c.provider}:${c.model}`, model, key, thinking: c.thinking, from: c.from };
	}

	async whyNot(c: Chosen, catalog?: Catalog): Promise<string | null> {
		if (c.provider === "faux" && !this.faux) return "faux models are for the tests (RESUMES_FAUX=1)";
		if (this.needsKey(c.provider) && !(await this.registry.getApiKeyForProvider(c.provider))) {
			return `no key for ${c.provider} on this server (${PROVIDERS[c.provider].env})`;
		}
		if (c.provider !== "openrouter" && c.provider !== "faux" && !this.registry.find(c.provider, c.model)) {
			if (!(c.provider === "openai" && catalog && (await catalog.row("openai", c.model)))) return `unknown model ${c.provider}/${c.model}`;
		}
		return null;
	}
}
