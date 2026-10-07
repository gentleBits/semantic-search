/** Small helpers of the turn: counts as the page shows them, the compact history, answer clean-ups, thinking levels, usage. */
import type { Usage as PiUsage } from "@earendil-works/pi-ai";

export interface Words {
	noun: string;
	nouns: string;
	document: string;
}

export const HISTORY_TURNS = 14;
export const HISTORY_CHARS = 700;
export const JD_MIN_CHARS = 400;
export const JD_MIN_LINES = 5;
export const THINKING = ["off", "minimal", "low", "medium", "high", "xhigh"] as const;
export type Thinking = (typeof THINKING)[number];

export const fmt = (n: number): string => n.toLocaleString("en-US");
export const people = (w: Words, n: number): string => `${fmt(n)} ${n === 1 ? w.noun : w.nouns}`;

/** `[web] effort` / the settings' thinking level → pi's: off, minimal, low, medium, high (xhigh: a few OpenAI models). */
export function thinkingLevel(effort: string | null | undefined): Thinking {
	const e = (effort || "off").trim().toLowerCase();
	if ((THINKING as readonly string[]).includes(e)) return e as Thinking;
	return ["none", "no", "0", ""].includes(e) ? "off" : "low";
}

export const COUNT = /^\s*\*\*\s*([\d][\d,.  ]*)\s+[^*\d]{0,24}\*\*/;

export function leadingCount(text: string): number | null {
	const m = COUNT.exec(text || "");
	if (!m) return null;
	const digits = m[1].replace(/\D/g, "");
	return digits ? parseInt(digits, 10) : null;
}

/** A sentence the model says twice in a row (a remark before the last step, then the answer) is shown once. */
export function once(text: string): string {
	const out: string[] = [];
	for (const line of (text || "").split("\n")) {
		const t = line.trim();
		if (t && out.length) {
			const prev = [...out].reverse().find((x) => x.trim());
			if (prev !== undefined && prev.trim() === t) continue;
		}
		out.push(line);
	}
	return out.join("\n").trim();
}

/** An answer that starts by copying the [screen] or [panel] line it was told is shown without it. */
export function unscreened(text: string): string {
	const lines = (text || "").split("\n");
	while (lines.length && /^\s*\[(screen|panel|check)\]/.test(lines[0])) lines.shift();
	while (lines.length && !lines[0].trim()) lines.shift();
	return lines.join("\n").trim();
}

export function looksLikeJd(text: string): boolean {
	const lines = text.split(/\r?\n/).filter((ln) => ln.trim());
	return text.length >= JD_MIN_CHARS && lines.length >= JD_MIN_LINES;
}

export interface ChatMessage {
	id?: string;
	ts?: string;
	role: string;
	text?: string;
	kind?: string;
	mono?: boolean;
	attachment?: { file: string; name: string; lines: number; chars: number; preview: string };
	steps?: { sign: string; text: string; detail: string }[];
	[k: string]: unknown;
}

export interface HistoryMessage {
	role: "user" | "assistant";
	content: string;
}

/** The chat in short (it seeds pi's transcript for a conversation that has none), and the [panel] line for what the
 * user did on the screen since the last answer. */
export function historyMessages(chat: ChatMessage[]): { history: HistoryMessage[]; panel: string } {
	const out: HistoryMessage[] = [];
	let notes: string[] = [];
	const cut = (t: string) => {
		t = t.trim();
		return t.length <= HISTORY_CHARS ? t : `${t.slice(0, HISTORY_CHARS).trimEnd()} …`;
	};
	for (const m of chat) {
		const role = m.role;
		if (role === "event") {
			if (m.kind !== "page") notes.push(String(m.text || "").replace("you ", "the user "));
		} else if (role === "error" || role === "info") {
			notes.push((role === "info" ? "the screen answered: " : "error shown: ") + String(m.text || "").replace(/\n/g, " | "));
		} else if (role === "user") {
			let text = String(m.text || "");
			if (m.attachment) text += `\n[attached job description, saved as ${m.attachment.file}]`;
			if (m.mono) {
				notes.push(`the user typed the command ${text}`);
				continue;
			}
			const head = notes.length ? `[panel] ${notes.join("; ")}\n` : "";
			notes = [];
			out.push({ role: "user", content: head + cut(text) });
		} else if (role === "assistant") {
			const did = (m.steps || []).map((s) => `${s.text || ""} ${s.detail || ""}`.trim()).join("; ");
			const text = cut(String(m.text || "")) + (did ? `\n(did: ${did})` : "");
			if (notes.length) {
				out.push({ role: "user", content: `[panel] ${notes.join("; ")}` });
				notes = [];
			}
			out.push({ role: "assistant", content: text || "(no answer)" });
		}
	}
	let keep = out.slice(-2 * HISTORY_TURNS);
	while (keep.length && keep[0].role !== "user") keep = keep.slice(1);
	return { history: keep, panel: notes.length ? `[panel] ${notes.join("; ")}\n` : "" };
}

export interface Usage {
	in: number;
	out: number;
	cached: number;
	calls: number;
	cost: number;
}

export const zeroUsage = (): Usage => ({ in: 0, out: 0, cached: 0, calls: 0, cost: 0 });

export function addUsage(total: Usage, u: PiUsage | undefined | null, calls = 1): Usage {
	if (u) {
		total.in += (u.input || 0) + (u.cacheWrite || 0); // pi-ai counts the prompt tokens written to the cache apart
		total.out += u.output || 0;
		total.cached += u.cacheRead || 0;
		total.cost += (u.cost && u.cost.total) || 0;
	}
	total.calls += calls;
	return total;
}

export function mergeUsage(total: Usage, u: Usage): Usage {
	total.in += u.in;
	total.out += u.out;
	total.cached += u.cached;
	total.calls += u.calls;
	total.cost += u.cost;
	return total;
}

export const roundCost = (u: Usage): Usage => ({ ...u, cost: Math.round(u.cost * 1e6) / 1e6 });
