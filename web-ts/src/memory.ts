/**
 * The agent's memory: one pi session per conversation in `<sessions>/<sid>/pi/` (the terminal `pi` format), appended as
 * the turn runs and compacted by pi past the window. The UI's `chat.jsonl` is separate.
 */
import { mkdirSync, readdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import type { AgentMessage, StreamFn } from "@earendil-works/pi-agent-core";
import type { Api, AssistantMessage, Message, Model } from "@earendil-works/pi-ai";
import { SessionManager, calculateContextTokens, estimateTokens, findCutPoint, generateSummary, getLastAssistantUsage, getLatestCompactionEntry, shouldCompact } from "@earendil-works/pi-coding-agent";
import type { Thinking } from "./text.js";

export interface CompactionSettings {
	enabled: boolean;
	reserveTokens: number;
	keepRecentTokens: number;
}

/** Tokens of transcript kept before pi's compaction summarises the older turns (a budget under the model's window). */
export const MEMORY_WINDOW = 120_000;

/** What pi's summary should keep of a resume search, instead of the files a coding session tracks. */
export const COMPACTION_FOCUS =
	"This is a conversation between a recruiter and the assistant of a resume search tool. Keep: every question the user asked, " +
	"the filters and rankings that were made with the counts they gave, what the sets looked like (countries and cities, rates, " +
	"years, seniority, top skills), which people were opened or named (#N and titles), what the user was told, and what is on " +
	"the screen now. No file operations: there are none.";

export interface Recall {
	sm: SessionManager;
	messages: AgentMessage[];
	model: { provider: string; modelId: string } | null;
	fresh: boolean;
}

export interface Compacted {
	tokensBefore: number;
	summary: string;
}

export class Memory {
	constructor(
		public sessionsDir: string,
		public window: number = MEMORY_WINDOW,
		public cwd: string = process.cwd(),
	) {}

	dir(sid: string): string {
		return join(this.sessionsDir, sid, "pi");
	}

	/** pi's compaction settings, scaled to the window (pi's own numbers for a 128k+ model). */
	settings(): CompactionSettings {
		return { enabled: true, reserveTokens: Math.min(16384, Math.round(this.window / 8)), keepRecentTokens: Math.min(20000, Math.round(this.window / 6)) };
	}

	open(sid: string): Recall {
		const dir = this.dir(sid);
		mkdirSync(dir, { recursive: true });
		// the conversation's own folder: its newest transcript, whatever folder the server runs in (pi's continueRecent
		// only finds those written from the same cwd)
		const newest = readdirSync(dir).filter((f) => f.endsWith(".jsonl")).sort().at(-1);
		const sm = newest ? SessionManager.open(join(dir, newest), dir, this.cwd) : SessionManager.create(this.cwd, dir);
		const ctx = sm.buildSessionContext();
		return { sm, messages: ctx.messages, model: ctx.model, fresh: sm.getEntries().length === 0 };
	}

	/** One message as pi emitted it → the transcript. A failed or stopped answer keeps its words, not the calls that never ran. */
	record(sm: SessionManager, m: AgentMessage): void {
		if (m.role === "assistant") {
			const a = m as AssistantMessage;
			if (a.stopReason === "error" || a.stopReason === "aborted") {
				const text = a.content.filter((c) => c.type === "text" && c.text.trim());
				if (!text.length) return;
				sm.appendMessage({ ...a, content: text, stopReason: "stop" } as Message);
				return;
			}
		}
		if (m.role === "user" || m.role === "assistant" || m.role === "toolResult") sm.appendMessage(m as Message);
	}

	/** How many tokens the transcript is: the model's last count when it gave one, or pi's estimate (chars / 4), the larger. */
	size(sm: SessionManager): number {
		const messages = sm.buildSessionContext().messages;
		const usage = getLastAssistantUsage(sm.getBranch());
		const counted = usage ? calculateContextTokens(usage) : 0;
		return Math.max(counted, messages.reduce((n, m) => n + estimateTokens(m), 0));
	}

	/** Over the window, the older turns become one summary (one model call through `stream`, the chat's pi collection);
	 * the cut moves back to a turn's start when it would split one. */
	async compactIfNeeded(sm: SessionManager, model: Model<Api>, apiKey: string | undefined, thinking: Thinking, stream: StreamFn, signal?: AbortSignal): Promise<Compacted | null> {
		const window = Math.min(this.window, model.contextWindow || this.window);
		const settings = this.settings();
		const tokensBefore = this.size(sm);
		if (!shouldCompact(tokensBefore, window, settings)) return null;
		const entries = sm.getBranch();
		if (!entries.length || entries[entries.length - 1].type === "compaction") return null;
		const previous = getLatestCompactionEntry(entries);
		let start = 0;
		if (previous) {
			const kept = entries.findIndex((e) => e.id === previous.firstKeptEntryId);
			start = kept >= 0 ? kept : entries.indexOf(previous) + 1;
		}
		const cut = findCutPoint(entries, start, entries.length, settings.keepRecentTokens);
		const end = cut.isSplitTurn && cut.turnStartIndex > start ? cut.turnStartIndex : cut.firstKeptEntryIndex;
		const first = entries[end];
		if (!first?.id || end <= start) return null;
		const older: AgentMessage[] = [];
		for (const e of entries.slice(start, end)) if (e.type === "message") older.push(e.message);
		if (!older.length) return null;
		const level: Thinking = thinking === "off" ? "off" : "low"; // a summary needs no long thought
		const summary = await generateSummary(older, model, settings.reserveTokens, apiKey || "", undefined, signal, COMPACTION_FOCUS, previous?.summary, level, stream);
		sm.appendCompaction(summary, first.id, tokensBefore);
		return { tokensBefore, summary };
	}

	delete(sid: string): void {
		rmSync(this.dir(sid), { recursive: true, force: true });
	}
}
