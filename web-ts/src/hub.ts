/**
 * What the app server keeps in memory: the engine, the settings, pi's registry, the providers' model lists, the login
 * and the email and phone check, and the running turns. The model is chosen per turn, so a settings change applies to the next message.
 */
import { join } from "node:path";
import { Auth, type Role } from "./auth.js";
import { Engine, type EngineConfig } from "./engine.js";
import { MEMORY_WINDOW, Memory } from "./memory.js";
import { Models, type ModelChoice } from "./models.js";
import { ResumesError } from "./errors.js";
import type { Feature } from "./features.js";
import { MailDomains, type MxLookup, systemMx } from "./mail-domains.js";
import { Owners } from "./owners.js";
import { Catalog, type Fetch, PROVIDERS, provider as providerOf } from "./providers.js";
import { type Policy, policyOf } from "./policy.js";
import { DryRun, type Http, type Senders, sendersFrom } from "./senders.js";
import { type Defaults, type Picks, Settings, UserPicks, merged, parseModel } from "./settings.js";
import { Signup } from "./signup.js";
import { type TurnDeps, runTurn, type Emit, type TurnInput } from "./turn.js";
import { UsageBook } from "./usage.js";

export interface HubOptions {
	engineUrl: string;
	stateDir: string; // `.resumes/`: settings.json, users.json, owners.json, secret, cache/
	faux?: boolean;
	fauxTokensPerSecond?: number;
	fetch?: Fetch;
	memoryWindow?: number; // tokens of transcript kept before pi compacts it
	now?: () => number;
	signup?: boolean; // on only when the senders can be made
	policy?: unknown;
	env?: Record<string, string | undefined>;
	http?: Http;
	senders?: Senders;
	mx?: MxLookup | null;
}

interface Running {
	controller: AbortController;
	startedAt: number;
}

export class Hub {
	readonly engine: Engine;
	readonly settings: Settings;
	readonly models: Models;
	readonly catalog: Catalog;
	readonly auth: Auth;
	readonly owners: Owners;
	readonly policy: Policy;
	readonly signup: Signup | null;
	readonly signupOff: string | null; // why the check is off although it was asked for
	readonly usage: UsageBook;
	readonly picks: UserPicks;
	features: Feature[] = []; // optional parts found at start (features.ts)
	readonly turns = new Map<string, Running>();
	perRound: number | null = null; // seconds one round of judging took, measured by the last job
	private memories = new Map<string, Memory>();

	constructor(public opts: HubOptions) {
		this.engine = new Engine(opts.engineUrl);
		this.policy = policyOf(opts.policy);
		const now = () => (opts.now ? opts.now() : Date.now());
		this.auth = new Auth(opts.stateDir, opts.now, () => !!this.signup, () => this.policy.limits);
		let signup: Signup | null = null;
		let off: string | null = null;
		if (opts.signup) {
			const made = opts.senders ? { senders: opts.senders, missing: [] } : sendersFrom(opts.env || process.env, opts.http);
			// the dry run mails nothing, so it skips DNS too (its tests use example.com, which takes no mail)
			const mx = opts.mx !== undefined ? opts.mx : made.senders instanceof DryRun ? null : systemMx();
			if (made.senders) signup = new Signup(opts.stateDir, this.auth.users, made.senders, () => this.policy.signup, () => this.auth.key(), now, new MailDomains(mx));
			else off = `no ${made.missing.join(", ")}`;
		}
		this.signup = signup;
		this.signupOff = off;
		this.usage = new UsageBook(opts.stateDir, () => this.policy.limits, now, (user) => this.auth.users.sharing(user));
		this.picks = UserPicks.in(opts.stateDir);
		this.owners = Owners.in(opts.stateDir);
		this.settings = Settings.in(opts.stateDir);
		this.models = new Models(this.settings);
		this.catalog = new Catalog(join(opts.stateDir, "cache"), opts.fetch, () => this.models.registry.getAll(), (p) => this.settings.key(p));
		if (opts.faux) this.models.enableFaux(opts.fauxTokensPerSecond);
	}

	/** The agent's memory: pi sessions next to the engine's conversation files (`<sessions>/<sid>/pi/`), else under the state dir. */
	memory(cfg: EngineConfig): Memory {
		const dir = cfg.sessions_dir || join(this.opts.stateDir, "agent");
		let m = this.memories.get(dir);
		if (!m) {
			m = new Memory(dir, this.opts.memoryWindow ?? MEMORY_WINDOW);
			this.memories.set(dir, m);
		}
		return m;
	}

	role(user: string | null | undefined): Role | null {
		return user && this.auth.on() ? this.auth.users.role(user) : null;
	}

	/** Members have an allowance and pick only priced models; admins and a server without login have neither limit. */
	limited(user: string | null | undefined): boolean {
		return this.role(user) === "member";
	}

	mine(user: string | null | undefined): Picks | null {
		return user && this.auth.on() ? this.picks.get(user) : null;
	}

	/** A model a member may run on: a known price, at most the ceiling (the faux model counts as priced). */
	private async affordable(provider: string, model: string): Promise<boolean> {
		if (provider === "faux") return true;
		try {
			const row = await this.catalog.row(provider, model);
			return !!row && row.in_per_m != null && row.out_per_m != null && row.out_per_m <= this.policy.limits.max_usd_per_m_out;
		} catch {
			return false;
		}
	}

	/** The picks a person's turns run on: their own, else the server's. For a member an unpriced or over-ceiling model is
	 * replaced by resumes.toml's: the server's default may be an admin's unpriced pick, which would count as $0. */
	async inForce(user: string | null | undefined, d: Defaults): Promise<Picks | null> {
		const own = this.mine(user);
		if (!this.limited(user)) return own;
		const c = this.settings.chatChoice(d, own);
		const j = this.settings.judgeChoice(d, own);
		const okChat = await this.affordable(c.provider, c.model);
		const okJudge = j.from === "chat" ? okChat : await this.affordable(j.provider, j.model);
		if (okChat && okJudge) return own;
		const [provider, model] = parseModel(d.model);
		return {
			chat: okChat ? { provider: c.provider, model: c.model, thinking: c.thinking } : { provider, model, thinking: c.thinking },
			judge: okJudge && j.from !== "chat" ? { provider: j.provider, model: j.model, thinking: j.thinking } : { thinking: j.thinking },
		};
	}

	private async defaults(cfg?: EngineConfig): Promise<Defaults> {
		return (cfg || (await this.engine.config())).defaults;
	}

	async chatModel(cfg?: EngineConfig, user?: string | null): Promise<ModelChoice> {
		const d = await this.defaults(cfg);
		return this.models.choice(this.settings.chatChoice(d, await this.inForce(user, d)), this.catalog);
	}

	async judgeModel(cfg?: EngineConfig, user?: string | null): Promise<ModelChoice> {
		const d = await this.defaults(cfg);
		return this.models.choice(this.settings.judgeChoice(d, await this.inForce(user, d)), this.catalog);
	}

	async whyNot(cfg?: EngineConfig, user?: string | null): Promise<string | null> {
		try {
			const d = await this.defaults(cfg);
			return await this.models.whyNot(this.settings.chatChoice(d, await this.inForce(user, d)), this.catalog);
		} catch (e) {
			return String((e as Error)?.message || e);
		}
	}

	async assistantFields(cfg?: EngineConfig, user?: string | null): Promise<{ model: string; judge: string; assistant: boolean; assistant_off: string | null }> {
		const d = await this.defaults(cfg);
		const mine = await this.inForce(user, d);
		const c = this.settings.chatChoice(d, mine);
		const j = this.settings.judgeChoice(d, mine);
		const off = await this.whyNot(cfg, user);
		return { model: `${c.provider}:${c.model}`, judge: `${j.provider}:${j.model}`, assistant: off === null, assistant_off: off };
	}

	async settingsView(user?: string | null): Promise<Record<string, unknown>> {
		const cfg = await this.engine.config();
		const a = await this.assistantFields(cfg, user);
		const role = this.role(user);
		return {
			...(await this.settings.view(cfg.defaults, await this.inForce(user, cfg.defaults))),
			model: a.model,
			judge_model: a.judge,
			assistant: a.assistant,
			assistant_off: a.assistant_off,
			yours: !!(user && this.auth.on()),
			priced_only: role === "member",
			...(role === "member" ? { allowance: { ...this.usage.day(user!), turns_per_day: this.policy.limits.turns_per_day, usd_per_day: this.policy.limits.usd_per_day, sessions: this.usage.sessionsToday(user!), sessions_per_day: this.policy.limits.sessions_per_day } } : {}),
		};
	}

	async applySettings(patch: Record<string, unknown>, user?: string | null): Promise<void> {
		await this.onlyOffered(patch, user);
		if (user && this.auth.on()) {
			const base = this.mine(user) || { chat: { ...this.settings.chat }, judge: { ...this.settings.judge } };
			const next = merged(patch, base);
			if (!next.chat.provider || !next.chat.model) {
				// a pick of the thinking level alone, before any model: it starts from the model in force
				const c = this.settings.chatChoice(await this.defaults());
				next.chat = { provider: c.provider, model: c.model, ...next.chat };
			}
			this.picks.set(user, next);
		} else this.settings.apply(patch);
	}

	async offeredTo(provider: string, user: string | null | undefined, refresh = false): Promise<Record<string, any>> {
		const listing = await this.catalog.list(provider, { refresh });
		if (!this.limited(user)) return listing;
		const cap = this.policy.limits.max_usd_per_m_out;
		return { ...listing, models: listing.models.filter((m) => m.in_per_m != null && m.out_per_m != null && m.out_per_m <= cap), priced_only: true, max_usd_per_m_out: cap };
	}

	providers(): typeof PROVIDERS {
		return PROVIDERS;
	}

	/** A settings change may name only a provider the server has a key for and a model of the list it offers. */
	async onlyOffered(patch: Record<string, unknown>, user?: string | null): Promise<void> {
		const configured = await this.settings.configured();
		const priced = this.limited(user);
		for (const what of ["chat", "judge"] as const) {
			const v = patch[what];
			if (!v || typeof v !== "object" || Array.isArray(v)) continue;
			const { provider, model } = v as { provider?: unknown; model?: unknown };
			if (!provider && !model) continue;
			const pid = providerOf(String(provider || "")).id;
			if (!configured.includes(pid)) throw new ResumesError("BAD_ARGUMENT", `no key for ${pid} on this server (${PROVIDERS[pid].env})`);
			const listing = await this.catalog.list(pid);
			if (listing.models.length && !listing.models.some((m) => m.id === String(model || ""))) {
				const what = pid === "openrouter" ? `the ${listing.models.length} free models the server offers on OpenRouter` : `the ${listing.models.length} models the server offers on ${PROVIDERS[pid].name} (pi's registry and OpenAI's list)`;
				throw new ResumesError("BAD_ARGUMENT", `${String(model || "")} is not one of ${what}`);
			}
			if (priced) {
				// an unpriced model would count as $0 and slip past the allowance; a very dear one would spend
				// dollars in one turn before the allowance is checked again
				const row = listing.models.find((m) => m.id === String(model || ""));
				if (!row || row.in_per_m == null || row.out_per_m == null) {
					throw new ResumesError("BAD_ARGUMENT", `${String(model || "")} has no known price, so it can't be counted against your allowance — pick a model with a price`);
				}
				const cap = this.policy.limits.max_usd_per_m_out;
				if (row.out_per_m > cap) throw new ResumesError("BAD_ARGUMENT", `${String(model || "")} costs $${row.out_per_m} per million output tokens; the limit for your account is $${cap}`);
			}
		}
	}

	busy(sid: string): boolean {
		return this.turns.has(sid);
	}

	stop(sid: string): boolean {
		const r = this.turns.get(sid);
		if (!r) return false;
		r.controller.abort();
		return true;
	}

	async turn(sid: string, input: TurnInput, emit: Emit, user: string | null = null): Promise<Record<string, any>> {
		if (this.turns.has(sid)) throw new Error("busy");
		const done = this.usage.begin(user, this.limited(user)); // throws USAGE_LIMIT / BUSY before anything is said or spent
		const controller = new AbortController();
		this.turns.set(sid, { controller, startedAt: Date.now() });
		try {
			const cfg = await this.engine.config();
			let off = await this.whyNot(cfg, user);
			let chat: ModelChoice | null = null;
			if (!off) {
				try {
					chat = await this.chatModel(cfg, user);
				} catch (e) {
					off = String((e as Error)?.message || e);
				}
			}
			const deps: TurnDeps = {
				engine: this.engine,
				config: cfg,
				chat,
				off,
				memory: this.memory(cfg),
				judge: () => this.judgeModel(cfg, user),
				perRound: (s) => {
					if (s) this.perRound = s;
				},
				spent: (usage) => this.usage.record(user, sid, usage, chat?.name || ""),
			};
			return await runTurn(deps, sid, input, emit, controller.signal);
		} finally {
			this.turns.delete(sid);
			done();
		}
	}
}
