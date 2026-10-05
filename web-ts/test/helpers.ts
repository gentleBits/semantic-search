// Test harness: the real engine on a free port, the app in-process, and pi-ai's faux model playing a script.
import { type ChildProcess, spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { type AssistantMessage, type Context as PiContext, type FauxResponseStep, type StreamOptions, fauxAssistantMessage } from "@earendil-works/pi-ai";
import type { Feature } from "../src/features.js";
import type { App } from "../src/server.js";
import type { EngineConfig } from "../src/engine.js";
import { Hub, type HubOptions } from "../src/hub.js";
import type { Fetch } from "../src/providers.js";
import { DryRun, type Senders } from "../src/senders.js";
import { createApp } from "../src/server.js";

export const ROOT = resolve(fileURLToPath(new URL("..", import.meta.url)), "..");
export const RESUMES = join(ROOT, ".venv", "bin", "resumes");
// the tests' own index (tests/testroot.py builds it): the public data plus the synthetic fixture
export const TEST_ROOT = process.env.RESUMES_TEST_ROOT || join(ROOT, ".test");
export const FIXTURES = join(ROOT, "tests", "fixtures");
export const CRITERION = "talented, decent price";

export function freePort(): Promise<number> {
	return new Promise((res, rej) => {
		const s = createServer();
		s.listen(0, "127.0.0.1", () => {
			const port = (s.address() as { port: number }).port;
			s.close(() => res(port));
		});
		s.on("error", rej);
	});
}

export interface EngineHandle {
	url: string;
	sessions: string;
	proc: ChildProcess;
	stop: () => void;
}

/** `resumes engine` on a free port with its own sessions directory; null when it cannot run (no venv, no index). */
export async function startEngine(): Promise<EngineHandle | null> {
	if (!existsSync(RESUMES)) return null;
	const port = await freePort();
	const sessions = mkdtempSync(join(tmpdir(), "resumes-web-test-"));
	const proc = spawn(RESUMES, ["engine", "--port", String(port), "--sessions", sessions], { cwd: existsSync(join(TEST_ROOT, "resumes.toml")) ? TEST_ROOT : ROOT, stdio: ["ignore", "ignore", "inherit"] });
	const url = `http://127.0.0.1:${port}`;
	const stop = () => {
		if (!proc.killed) proc.kill("SIGTERM");
	};
	const deadline = Date.now() + 60_000;
	while (Date.now() < deadline) {
		if (proc.exitCode !== null) return null;
		try {
			const r = await fetch(`${url}/api/config`);
			if (r.status === 200) return { url, sessions, proc, stop };
			if (r.status === 503) {
				stop();
				return null; // no index built
			}
		} catch {
			// not up yet
		}
		await new Promise((r) => setTimeout(r, 100));
	}
	stop();
	return null;
}

/** One answer of the model: text, tool calls, or a whole message; `delayMs` waits first (honouring the abort). */
export type Step = string | [string, Record<string, unknown>][] | { content?: AssistantMessage["content"]; text?: string; stopReason?: AssistantMessage["stopReason"]; errorMessage?: string; delayMs?: number };

export interface Call {
	system?: string;
	messages?: PiContext["messages"];
	tools?: string[];
	judge?: string[];
	criterion?: string;
	summary?: string; // pi's compaction asked for a summary of this conversation text
}

const sleep = (ms: number, signal?: AbortSignal) =>
	new Promise<void>((res) => {
		const t = setTimeout(res, ms);
		signal?.addEventListener("abort", () => {
			clearTimeout(t);
			res();
		});
	});

let fauxCalls = 0;

function messageOf(step: Step): AssistantMessage {
	if (typeof step === "string") return fauxAssistantMessage(step);
	if (Array.isArray(step)) {
		return fauxAssistantMessage(
			step.map(([name, args], i) => ({ type: "toolCall" as const, id: `faux_${Date.now()}_${fauxCalls++}_${i}`, name, arguments: args })),
			{ stopReason: "toolUse" },
		);
	}
	const content = step.content ?? (step.text !== undefined ? [{ type: "text" as const, text: step.text }] : []);
	const stopReason = step.stopReason ?? (step.errorMessage ? "error" : content.some((c) => c.type === "toolCall") ? "toolUse" : "stop");
	return fauxAssistantMessage(content, { stopReason, errorMessage: step.errorMessage });
}

export interface ApiOptions {
	fetch?: Fetch;
	parallel?: number;
	model?: string; // the engine config's default chat model (faux:faux if unset)
	faux?: boolean;
	env?: Record<string, string | undefined>;
	memoryWindow?: number; // tokens of transcript before pi compacts it
	sid?: string; // reopen this conversation, as after a restart of the app server
	signup?: boolean; // sign-up on, with `senders` or else the dry run
	senders?: Senders;
	policy?: unknown;
	now?: () => number;
	features?: (hub: Hub) => Feature[]; // none by default
}

export const SUMMARY = "Summary by the faux model: the user asked who worked on data pipelines (165 people) and was told the set is too big to rank.";


export class Api {
	hub: Hub;
	app: App;
	sid = "";
	headers: Record<string, string> = {}; // sent with every request, e.g. the login cookie
	calls: Call[] = [];
	script: Step[] = [];
	judge: (criterion: string, ids: string[]) => { id: string; score: number; note: string }[] = (_c, ids) =>
		ids.map((id) => ({ id, score: 40 + ((parseInt(id.slice(1), 10) * 7) % 60), note: `scripted note for ${id}` }));

	constructor(
		public engine: EngineHandle,
		public stateDir: string = mkdtempSync(join(tmpdir(), "resumes-web-state-")),
		public opts: ApiOptions = {},
	) {
		const hubOpts: HubOptions = {
			engineUrl: engine.url,
			stateDir,
			faux: opts.faux ?? true,
			fauxTokensPerSecond: 300,
			fetch: opts.fetch,
			memoryWindow: opts.memoryWindow,
			signup: opts.signup,
			senders: opts.signup ? opts.senders || new DryRun(false) : undefined,
			policy: opts.policy,
			now: opts.now,
		};
		if (opts.sid) this.sid = opts.sid;
		this.hub = new Hub(hubOpts);
		this.hub.features = opts.features ? opts.features(this.hub) : [];
		const orig = this.hub.engine.config.bind(this.hub.engine);
		this.hub.engine.config = async (): Promise<EngineConfig> => {
			const c = await orig();
			return { ...c, defaults: { ...c.defaults, model: opts.model || "faux:faux", judge_model: null }, judge: { ...c.judge, parallel: opts.parallel ?? c.judge.parallel } };
		};
		this.app = createApp(this.hub);
		if (this.hub.models.faux) this.arm();
	}

	/** The faux model answers every call from here: a judge call with generated scores, anything else with the next step of the script. */
	private arm(): void {
		const faux = this.hub.models.faux!;
		const respond = async (context: PiContext, options: StreamOptions | undefined): Promise<AssistantMessage> => {
			const tools = (context.tools || []).map((t) => t.name);
			const user = [...context.messages].reverse().find((m) => m.role === "user");
			const text = user ? (typeof user.content === "string" ? user.content : user.content.map((c) => (c.type === "text" ? c.text : "")).join("")) : "";
			if ((context.systemPrompt || "").startsWith("You are a context summarization assistant")) {
				this.calls.push({ summary: text });
				return fauxAssistantMessage(SUMMARY);
			}
			if (tools.includes("submit_scores")) {
				const criterion = text.includes("Criterion: ") ? text.split("Criterion: ")[1].split("\n")[0] : "";
				const m = /Cards to score \(\d+\): ([^\n]+)/.exec(text);
				const ids = m ? m[1].split(", ").map((s) => s.trim()) : [];
				this.calls.push({ judge: ids, criterion });
				if (this.opts.parallel !== undefined) await sleep(250, options?.signal);
				if (options?.signal?.aborted) return fauxAssistantMessage([], { stopReason: "aborted", errorMessage: "Request was aborted" });
				return fauxAssistantMessage([{ type: "toolCall", id: `judge_${fauxCalls++}`, name: "submit_scores", arguments: { scores: this.judge(criterion, ids) } }], { stopReason: "toolUse" });
			}
			this.calls.push({ system: context.systemPrompt, messages: context.messages, tools });
			const step: Step = this.script.length ? this.script.shift()! : "ok";
			if (typeof step === "object" && !Array.isArray(step) && step.delayMs) await sleep(step.delayMs, options?.signal);
			if (options?.signal?.aborted) return fauxAssistantMessage([], { stopReason: "aborted", errorMessage: "Request was aborted" });
			return messageOf(step);
		};
		faux.setResponses(Array.from({ length: 500 }, () => respond as FauxResponseStep));
	}

	async init(): Promise<this> {
		if (this.sid) return this;
		const r = await this.request("POST", "/api/sessions", {});
		if (r.status !== 201) throw new Error(`no session: ${r.status} ${r.text}`);
		this.sid = r.body.session.id;
		return this;
	}

	async request(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<{ status: number; body: any; text: string; headers: Headers }> {
		const raw = body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body);
		const res = await this.app.request(path, {
			method,
			headers: { host: "localhost", ...this.headers, ...(raw !== undefined ? { "content-type": "application/json", "content-length": String(Buffer.byteLength(raw)) } : {}), ...headers },
			body: raw,
		});
		const text = await res.text();
		let parsed: any = null;
		try {
			parsed = text ? JSON.parse(text) : null;
		} catch {
			parsed = null;
		}
		return { status: res.status, body: parsed, text, headers: res.headers };
	}

	get(path = "") {
		return this.request("GET", `/api/sessions/${this.sid}${path}`);
	}
	act(action: Record<string, unknown>) {
		return this.request("POST", `/api/sessions/${this.sid}/action`, action);
	}
	stop() {
		return this.request("POST", `/api/sessions/${this.sid}/stop`, {});
	}

	/** One turn → the events, in order. */
	async say(text = "", body: Record<string, unknown> = {}): Promise<Record<string, any>[]> {
		const r = await this.request("POST", `/api/sessions/${this.sid}/chat`, { text, ...body });
		if (r.status !== 200) throw new Error(`chat answered ${r.status}: ${r.text}`);
		const events: Record<string, any>[] = [];
		for (const block of r.text.split("\n\n")) {
			const line = block.split("\n").find((l) => l.startsWith("data: "));
			if (line) events.push(JSON.parse(line.slice(6)));
		}
		return events;
	}

	/** The transcript's chat calls only (not the judge's). */
	get chatCalls(): Call[] {
		return this.calls.filter((c) => c.messages);
	}
	get judgeCalls(): Call[] {
		return this.calls.filter((c) => c.judge);
	}
	get summaryCalls(): Call[] {
		return this.calls.filter((c) => c.summary !== undefined);
	}
	/** The transcript pi keeps for this conversation (`<sessions>/<sid>/pi/*.jsonl`): its entries. */
	piEntries(): Record<string, any>[] {
		const dir = join(this.engine.sessions, this.sid, "pi");
		if (!existsSync(dir)) return [];
		return readdirSync(dir)
			.filter((f) => f.endsWith(".jsonl"))
			.flatMap((f) => readFileSync(join(dir, f), "utf-8").split("\n").filter(Boolean).map((l) => JSON.parse(l)));
	}
}

export const kinds = (events: Record<string, any>[]) => events.map((e) => e.type as string);
export const last = (events: Record<string, any>[], kind: string) => events.filter((e) => e.type === kind).at(-1)!;
/** The text of the last message the model was given (a user message or a tool result). */
export const told = (call: Call): string => {
	const m = call.messages!.at(-1)!;
	if (m.role === "toolResult") return m.content.map((c) => (c.type === "text" ? c.text : "")).join("");
	if (m.role === "user") return typeof m.content === "string" ? m.content : m.content.map((c) => (c.type === "text" ? c.text : "")).join("");
	return "";
};

export function stubFetch(routes: Record<string, ((headers: Record<string, string>) => { status: number; body: any }) | { status: number; body: any }>): Fetch {
	return async (url, headers = {}) => {
		for (const [prefix, answer] of Object.entries(routes)) {
			if (url.startsWith(prefix)) return typeof answer === "function" ? answer(headers) : answer;
		}
		const { ResumesError } = await import("../src/errors.js");
		throw new ResumesError("PROVIDER_UNREACHABLE", `${new URL(url).host}: no network`);
	};
}
