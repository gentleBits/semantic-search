/**
 * One turn of the assistant: pi-agent-core's `Agent` runs the loop in-process with the engine's verbs as tools. Events
 * to the browser: user, delta, interim (the text so far was a remark before a tool call), step, state, rank, open,
 * status, done, error, memory.
 */
import { Agent, type AgentMessage } from "@earendil-works/pi-agent-core";
import type { Api, AssistantMessage, Model, UserMessage } from "@earendil-works/pi-ai";
import { convertToLlm } from "@earendil-works/pi-coding-agent";
import type { Engine, EngineConfig, EngineEvent, RankSummary, Snapshot } from "./engine.js";
import { EngineError, ResumesError } from "./errors.js";
import type { Memory } from "./memory.js";
import type { ModelChoice } from "./models.js";
import { agentTools, systemPrompt, toolSpecs } from "./prompt.js";
import { type RankContext, runRanking } from "./rank.js";
import { type HistoryMessage, type Usage, type Words, addUsage, fmt, historyMessages, leadingCount, looksLikeJd, once, people, roundCost, unscreened, zeroUsage } from "./text.js";

export const MAX_ROUNDS = 8;

export type Emit = (ev: Record<string, unknown>) => void;

export interface TurnInput {
	text?: string;
	attachment?: { name?: string | null; text?: string } | null;
	auto?: string | null;
}

export interface TurnDeps {
	engine: Engine;
	config: EngineConfig;
	chat: ModelChoice | null; // null: the chat cannot run, and `off` says why
	off: string | null;
	memory: Memory;
	judge: () => Promise<ModelChoice>;
	perRound: (seconds: number | null) => void;
	spent?: (usage: Usage) => void; // the turn's tokens and cost (its ranking included), once, whatever its end
}

const chipText = (v: Record<string, any>): string => (v.kind ? `${v.kind}: ${v.value}` : String(v.value));

function toPiMessages(history: HistoryMessage[], model: Model<Api>): AgentMessage[] {
	return history.map((m) =>
		m.role === "assistant"
			? ({
					role: "assistant",
					content: [{ type: "text", text: m.content }],
					api: model.api,
					provider: model.provider,
					model: model.id,
					usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
					stopReason: "stop",
					timestamp: Date.now(),
				} satisfies AssistantMessage)
			: ({ role: "user", content: m.content, timestamp: Date.now() } satisfies UserMessage),
	);
}

export class Turn implements RankContext {
	steps: { sign: string; text: string; detail: string }[] = [];
	added: string[] = [];
	tooMany = false;
	ranked: RankSummary | null = null;
	opened: string | null = null;
	corrected: { said: number; is: number } | null = null; // the check ran: what the answer opened with, what the set is
	usage: Usage = zeroUsage();
	piece = ""; // the text streamed since the last tool call
	last: AssistantMessage | null = null;
	rounds = 0;
	exhausted = false;
	fatal: Error | null = null; // the engine went away under a tool call: the turn ends
	compacted: { tokensBefore: number; summary: string } | null = null;

	constructor(
		public deps: TurnDeps,
		public sid: string,
		public emit: Emit,
		public signal: AbortSignal,
	) {}

	get engine(): Engine {
		return this.deps.engine;
	}
	get words(): Words {
		return this.deps.config;
	}
	get judgeParams() {
		return this.deps.config.judge;
	}
	judge(): Promise<ModelChoice> {
		return this.deps.judge();
	}
	perRound(seconds: number | null): void {
		this.deps.perRound(seconds);
	}

	forward(events: EngineEvent[]): void {
		for (const ev of events) {
			if (ev.type === "step") this.steps.push(ev.step);
			this.emit(ev as unknown as Record<string, unknown>);
		}
	}

	/** One tool call of the model → the text it reads. Throws for what pi should report as a tool error. */
	async call(name: string, args: Record<string, unknown>): Promise<string> {
		if (this.signal.aborted && name !== "rank") throw new Error("ERROR stopped by the user");
		if (this.piece.trim()) this.emit({ type: "interim", text: this.piece.trim() });
		this.piece = "";
		if (name === "rank") return runRanking(this, args as { criterion?: string; fresh?: boolean });
		let out: Awaited<ReturnType<Engine["tool"]>>;
		try {
			out = await this.engine.tool(this.sid, name, args);
		} catch (e) {
			if (e instanceof EngineError && (e.status === 0 || e.status >= 500)) {
				this.fatal = e;
				throw e;
			}
			throw e;
		}
		this.forward(out.events);
		if (out.meta.added?.length) this.added.push(...out.meta.added);
		if (out.meta.opened) this.opened = out.meta.opened;
		if (out.is_error) throw new Error(out.result);
		return out.result;
	}
}

/** Cues, options and buttons under the answer: from the engine's facts, not from the model's words. */
export async function decorate(turn: Turn, snap: Snapshot, text: string): Promise<Record<string, any>> {
	const msg: Record<string, any> = { role: "assistant", text: text.trim(), steps: turn.steps };
	const byId = new Map(snap.filters.map((f) => [f.id as string, f]));
	const cues: Record<string, unknown>[] = [];
	for (const fid of turn.added) {
		const f = byId.get(fid);
		if (!f) continue;
		if (f.terms?.length) {
			cues.push({
				kind: "understood",
				terms: f.terms.map((t: any) => ({ kind: t.kind, name: t.name, expanded: t.expanded, more: t.more })),
				by_meaning: f.by_meaning || 0,
				text: (f.args || {}).text ?? null,
			});
		} else if (f.kind === "text") cues.push({ kind: "meaning", value: f.value, by_meaning: f.by_meaning || 0 });
		else if (f.kind === "like" && f.like) cues.push({ kind: "like", ...f.like });
		if (f.unresolved?.length) cues.push({ kind: "unresolved", words: f.unresolved });
		const u = f.unknown || {};
		if (u.count && !u.included) cues.push({ kind: "unknown", count: u.count, what: u.what, id: fid });
		if (f.kind === "rate" && f.estimated) cues.push({ kind: "estimated", count: f.estimated, of: snap.set.count });
	}
	if (turn.ranked && !cues.some((c) => c.kind === "estimated")) {
		const est = snap.overview?.rate?.estimated;
		if (est) cues.push({ kind: "estimated", count: est, of: snap.set.count });
	}
	if (cues.length) msg.cues = cues;
	if (turn.tooMany && snap.too_many) msg.chips = snap.too_many.suggestions.slice(0, 6);
	const acts: Record<string, unknown>[] = [];
	const r = snap.ranking;
	const n = snap.set.count;
	if (r && !turn.ranked && 0 < r.judged && r.judged < n && n <= snap.rank.limit && turn.steps.length) {
		const fresh = n - r.judged;
		const est = snap.rank.estimate;
		acts.push({ label: `Rank the ${fresh} new`, star: true, say: `Rank the ${fresh} new.`, hint: `≈ ${Math.trunc(est)}–${Math.trunc(est * 1.5)} s` });
	}
	if (turn.ranked?.stopped && turn.ranked.judged < turn.ranked.total) {
		const left = turn.ranked.total - turn.ranked.judged;
		acts.push({ label: `Rank the ${left} left`, star: true, say: `Rank the ${left} left.` });
	}
	if (n === 0 && snap.zero) {
		for (const o of snap.zero.options.slice(0, 3)) acts.push({ label: `Remove ${chipText(o)}`, count: o.without, action: { type: "drop", targets: [o.id] } });
	}
	if (acts.length) msg.actions = acts;
	if (turn.ranked) msg.rank = turn.ranked;
	const places: number[] = [];
	for (const m of (msg.text as string).matchAll(/#(\d{1,4})\b/g)) {
		const k = parseInt(m[1], 10);
		if (k >= 1 && k <= n && k <= 200 && !places.includes(k)) places.push(k);
	}
	if (places.length) {
		const ids = await turn.engine.ids(turn.sid, places);
		const refs: Record<string, string | null> = {};
		for (const k of places) refs[String(k)] = ids[String(k)] ?? null;
		msg.refs = refs;
	}
	if (turn.opened) msg.opened = turn.opened;
	return msg;
}

/** The assistant answers one message. `attachment`: {name, text} of a job description. `auto`: a turn the screen
 * starts by itself ("rank_pending": a suggestion made the set small enough; "handoff": a slash command's words). */
export async function runTurn(deps: TurnDeps, sid: string, input: TurnInput, emit: Emit, signal: AbortSignal): Promise<Record<string, any>> {
	const { engine, config, chat } = deps;
	const started = Date.now();
	const turn = new Turn(deps, sid, emit, signal);
	const auto = input.auto || null;
	let text = (input.text || "").trim();
	let attachment = input.attachment || null;
	if (!attachment && looksLikeJd(text) && !auto) {
		attachment = { name: null, text };
		text = "";
	}
	const opened = await engine.session(sid);
	const { history, panel } = historyMessages(opened.chat);
	const snap = opened.state;
	turn.compacted = null;
	let att: Record<string, any> | null = null;
	if (!auto) {
		const body: Record<string, unknown> = { role: "user", text };
		if (attachment && (attachment.text || "").trim()) body.attachment = { name: attachment.name ?? null, text: attachment.text };
		const user = (await engine.message(sid, body)).message;
		att = user.attachment || null;
		emit({ type: "user", message: user });
	}
	let said: string;
	if (auto === "rank_pending") said = "[panel] the set is small enough for the pending ranking now: rank it.";
	else {
		said = text || "Who fits this job description best?";
		if (att) said += `\n[attached job description “${att.name}”, saved as ${att.file} (${att.lines} lines). It begins: ${att.preview}]`;
	}
	if (!chat) {
		const snapNow = (await engine.state(sid)).state;
		const err = (await engine.message(sid, { role: "error", code: "ASSISTANT_UNAVAILABLE", text: `The assistant cannot be reached${deps.off ? ` (${deps.off})` : ""}. The list, the filters and the slash commands work without it.` })).message;
		emit({ type: "error", message: err, state: snapNow });
		return err;
	}

	// A conversation with no pi transcript yet is seeded from its chat; a change of model is noted as pi notes it.
	const recall = deps.memory.open(sid);
	let messages = recall.messages;
	if (recall.fresh && history.length) {
		messages = toPiMessages(history, chat.model);
		for (const m of messages) recall.sm.appendMessage(m as UserMessage | AssistantMessage);
	}
	if (!recall.model || recall.model.provider !== chat.provider || recall.model.modelId !== chat.id) recall.sm.appendModelChange(chat.provider, chat.id);

	// Tool calls come to Turn.call one by one; every message pi emits goes to the transcript. A second prompt happens
	// only for the count check.
	const agent = new Agent({
		initialState: { systemPrompt: systemPrompt(config), model: chat.model, thinkingLevel: chat.thinking, tools: agentTools(toolSpecs(config), (n, a) => turn.call(n, a)), messages },
		convertToLlm,
		getApiKey: () => chat.key,
		toolExecution: "sequential",
		afterToolCall: async () => {
			if (turn.rounds + 1 >= MAX_ROUNDS) {
				turn.exhausted = true; // the model would want yet another round: that is where MAX_ROUNDS stops it
				return { terminate: true };
			}
			return undefined;
		},
	});
	agent.subscribe((ev) => {
		if (ev.type === "message_update") {
			const e = ev.assistantMessageEvent;
			if (e.type === "text_delta") {
				turn.piece += e.delta;
				emit({ type: "delta", text: e.delta });
			}
		} else if (ev.type === "message_end") {
			deps.memory.record(recall.sm, ev.message);
			if (ev.message.role === "assistant") {
				turn.last = ev.message;
				addUsage(turn.usage, ev.message.usage);
			}
		} else if (ev.type === "turn_end") {
			turn.rounds += 1;
		}
	});
	const onAbort = () => agent.abort();
	signal.addEventListener("abort", onAbort);
	if (signal.aborted) agent.abort();

	let final = "";
	let stopped = false;
	let failure: ResumesError | EngineError | null = null;
	let checked = false;
	let prompt: AgentMessage = { role: "user", content: `${snap.screen}\n${panel}${said}`, timestamp: Date.now() };
	try {
		for (;;) {
			turn.piece = "";
			turn.last = null;
			turn.exhausted = false;
			if (signal.aborted) {
				// a stop that came while the turn was getting ready: the model is not called at all
				final = turn.piece;
				stopped = true;
				break;
			}
			try {
				await agent.prompt(prompt);
			} catch (e) {
				throw new ResumesError("ASSISTANT_UNAVAILABLE", String((e as Error)?.message || e).slice(0, 300));
			}
			if (turn.fatal) throw turn.fatal;
			const last = turn.last as AssistantMessage | null; // set by the subscriber while the prompt ran
			if (last?.stopReason === "error") throw new ResumesError("ASSISTANT_UNAVAILABLE", (last.errorMessage || agent.state.errorMessage || "the model failed").slice(0, 300));
			if (last?.stopReason === "aborted" || signal.aborted) {
				final = turn.piece;
				stopped = true;
				break;
			}
			if (turn.exhausted) {
				final = "I could not finish that in a few steps — the list on the right shows where it stands.";
				break;
			}
			final = unscreened(turn.piece);
			const saidN = leadingCount(final);
			if (saidN !== null && !checked) {
				const n = (await engine.state(sid, false)).state.set.count;
				if (saidN !== n) {
					// a bold count opening the answer reads as the set's count; the model is told the real one and decides
					// whether it meant a part ("**44 of the 165**…") or a wrong count. Its second answer stands.
					checked = true;
					turn.corrected = { said: saidN, is: n };
					emit({ type: "interim", text: "" });
					prompt = {
						role: "user",
						content:
							`[check] the screen shows ${people(config, n)}; your answer opens with **${fmt(saidN)}**, which reads as the count of the set. ` +
							`If ${fmt(saidN)} counts a part of the set, say the answer again so that it reads so (“**${fmt(saidN)} of the ${fmt(n)}** …”); if you meant the set, say it again with ${fmt(n)}. Same short form.`,
						timestamp: Date.now(),
					};
					continue;
				}
			}
			break;
		}
	} catch (e) {
		if (e instanceof ResumesError || e instanceof EngineError) failure = e;
		else throw e;
	} finally {
		signal.removeEventListener("abort", onAbort);
	}
	turn.usage = roundCost(turn.usage);
	deps.spent?.(turn.usage);

	const snapAfter = (await engine.state(sid)).state;
	if (failure) {
		const words =
			failure instanceof ResumesError && failure.code === "ASSISTANT_UNAVAILABLE"
				? `The assistant cannot be reached${failure.message ? ` (${failure.message})` : ""}. The list, the filters and the slash commands work without it.`
				: failure instanceof EngineError
					? failure.text
					: failure.message || failure.code;
		const code = failure instanceof EngineError ? failure.code : failure.code;
		const err = (await engine.message(sid, { role: "error", code, text: words, ...(turn.steps.length ? { steps: turn.steps } : {}) })).message;
		emit({ type: "error", message: err, state: snapAfter });
		return err;
	}
	if (stopped) {
		const r = turn.ranked;
		final = r
			? `Stopped — **${r.judged} of ${fmt(r.total)}** are ranked for “${r.criterion}”. The rest are marked “not ranked yet”; their order comes after the ranked.`
			: `${final.trim() ? `${final.trim()} ` : ""}(stopped)`;
	}
	final = once(unscreened(final));
	const msg = await decorate(turn, snapAfter, final || "Done — the list on the right is up to date.");
	if (turn.corrected) msg.corrected = turn.corrected;
	Object.assign(msg, { seconds: Math.round((Date.now() - started) / 100) / 10, usage: turn.usage, model: chat.name }, stopped ? { stopped: true } : {}, auto ? { auto } : {});
	const saved = (await engine.message(sid, msg)).message;
	emit({ type: "done", message: saved, state: snapAfter });

	// past the window, pi summarises the older turns (one model call, after the answer is on the screen)
	try {
		turn.compacted = await deps.memory.compactIfNeeded(recall.sm, chat.model, chat.key, chat.thinking);
		if (turn.compacted) emit({ type: "memory", compacted: { tokens_before: turn.compacted.tokensBefore, summary_chars: turn.compacted.summary.length } });
	} catch (e) {
		emit({ type: "memory", error: String((e as Error)?.message || e).slice(0, 200) }); // the transcript stays whole; the next turn tries again
	}
	return saved;
}
