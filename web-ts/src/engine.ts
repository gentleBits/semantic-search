/**
 * The Python engine over localhost HTTP, one function per route. An error answer is thrown as `EngineError` with its
 * status and body, so the routes that proxy can hand it to the browser unchanged.
 */
import { EngineError } from "./errors.js";

export interface EngineConfig {
	name: string;
	noun: string;
	nouns: string;
	document: string;
	starters: string[];
	attach?: string; // what an attached file usually is here ("a job description"); older engines omit it
	count: number;
	documents: number;
	index_version: string;
	built_at: string | null;
	currency: string;
	symbol: string;
	limit: number;
	page_size: number;
	sort_keys: { key: string; name: string; dir: string }[];
	seniorities: string[];
	availabilities: { code: string; label: string }[];
	commands: string;
	changes: string[];
	topics: string[];
	fields?: Fields; // older engines omit it
	sessions_dir?: string; // the agent's memory lives next to a conversation's files
	defaults: { model: string; effort: string | null; judge_model: string | null; judge_effort: string | null };
	judge: { batch: number; parallel: number; note_max: number };
}

export interface Fields {
	people: number;
	location: { stated: number; examples: string[] };
	rate: { stated: number; estimated: number; unknown: number };
	years: { known: number; unknown: number };
	availability: { stated: number; values: [string, number][] };
	remote: { stated: number };
}

export type Snapshot = {
	session: { id: string; title: string | null };
	set: { id: string; count: number; page: number; pages: number; start: number; end: number; [k: string]: unknown };
	filters: Record<string, any>[];
	sort: { label: string; [k: string]: unknown };
	ranking: { judgment: string; criterion: string; judged: number; total: number; sorted: boolean } | null;
	ranking_off: boolean;
	rank: { limit: number; possible: boolean; unranked: number; pending: string | null; estimate: number; running: Record<string, unknown> | null };
	too_many: { count: number; limit: number; pending: string; suggestions: Record<string, any>[] } | null;
	items: Record<string, any>[];
	overview?: Record<string, any>;
	zero?: { options: Record<string, any>[]; [k: string]: unknown };
	screen: string;
	[k: string]: unknown;
};

export interface ToolAnswer {
	result: string;
	is_error: boolean;
	events: EngineEvent[];
	meta: { added?: string[]; opened?: string | null; too_many?: boolean };
}

export type EngineEvent =
	| { type: "step"; step: { sign: string; text: string; detail: string } }
	| { type: "state"; state: Snapshot }
	| { type: "open"; id: string; person: Record<string, any> };

export interface RankJob {
	set: string;
	criterion: string;
	into: string | null;
	total: number;
	already: number;
	context: string;
	anchors: string[];
	estimate: number;
	people: { doc: number; id: string; card: string }[];
}

export interface RankSummary {
	criterion: string;
	scored: number;
	asked: number;
	already: number;
	judged: number;
	total: number;
	seconds: number;
	stopped: boolean;
	judgment: string | null;
	error: string | null;
}

export type RankPrepared =
	| { job: RankJob; events: EngineEvent[] }
	| { refused: { result: string; events: EngineEvent[]; meta: { too_many: true } } }
	| { done: { result: string; events: EngineEvent[]; summary: RankSummary } }
	| { error: { code: string; message: string } };

export interface Score {
	id: string;
	score: number;
	note: string;
}

export class Engine {
	constructor(public url: string) {
		this.url = url.replace(/\/+$/, "");
	}

	private async call(method: string, path: string, body?: unknown): Promise<any> {
		let res: Response;
		try {
			res = await fetch(this.url + path, {
				method,
				headers: body !== undefined ? { "content-type": "application/json" } : {},
				body: body !== undefined ? JSON.stringify(body) : undefined,
			});
		} catch (e) {
			throw new EngineError(0, { error: { role: "error", code: "ENGINE_DOWN", text: `The search engine does not answer (${String((e as Error)?.message || e)}).` } });
		}
		const text = await res.text();
		let data: any;
		try {
			data = text ? JSON.parse(text) : {};
		} catch {
			data = { error: { role: "error", code: "ENGINE_BAD_ANSWER", text: `The search engine answered ${res.status} with something that is not JSON.` } };
		}
		if (!res.ok) throw new EngineError(res.status, data);
		return data;
	}

	config(): Promise<EngineConfig> {
		return this.call("GET", "/api/config");
	}
	sessions(): Promise<{ sessions: Record<string, any>[] }> {
		return this.call("GET", "/api/sessions");
	}
	newSession(): Promise<Record<string, any>> {
		return this.call("POST", "/api/sessions", {});
	}
	session(sid: string): Promise<{ session: { id: string; title: string | null }; chat: any[]; busy: boolean; state: Snapshot }> {
		return this.call("GET", `/api/sessions/${encodeURIComponent(sid)}`);
	}
	deleteSession(sid: string): Promise<{ deleted: string }> {
		return this.call("DELETE", `/api/sessions/${encodeURIComponent(sid)}`);
	}
	state(sid: string, overview = true): Promise<{ state: Snapshot; busy: boolean }> {
		return this.call("GET", `/api/sessions/${encodeURIComponent(sid)}/state${overview ? "" : "?overview=0"}`);
	}
	action(sid: string, action: unknown): Promise<Record<string, any>> {
		return this.call("POST", `/api/sessions/${encodeURIComponent(sid)}/action`, action);
	}
	person(sid: string, ref: string): Promise<Record<string, any>> {
		return this.call("GET", `/api/sessions/${encodeURIComponent(sid)}/people/${encodeURIComponent(ref)}`);
	}
	async personText(sid: string, ref: string): Promise<Response> {
		return fetch(`${this.url}/api/sessions/${encodeURIComponent(sid)}/people/${encodeURIComponent(ref)}/text`);
	}
	ids(sid: string, places: number[]): Promise<Record<string, string>> {
		return this.call("GET", `/api/sessions/${encodeURIComponent(sid)}/ids?places=${places.join(",")}`);
	}
	message(sid: string, message: Record<string, unknown>): Promise<{ message: Record<string, any> }> {
		return this.call("POST", `/api/sessions/${encodeURIComponent(sid)}/messages`, { message });
	}
	slash(sid: string, text: string): Promise<Record<string, any>> {
		return this.call("POST", `/api/sessions/${encodeURIComponent(sid)}/slash`, { text });
	}
	tool(sid: string, name: string, args: unknown): Promise<ToolAnswer> {
		return this.call("POST", `/api/sessions/${encodeURIComponent(sid)}/tool`, { name, args });
	}
	rankPrepare(sid: string, args: { criterion?: string; fresh?: boolean }): Promise<RankPrepared> {
		return this.call("POST", `/api/sessions/${encodeURIComponent(sid)}/rank/prepare`, args);
	}
	rankProgress(sid: string, scores: Score[]): Promise<{ done: number; total: number; seconds: number }> {
		return this.call("POST", `/api/sessions/${encodeURIComponent(sid)}/rank/progress`, { scores });
	}
	rankFinish(
		sid: string,
		body: { scores: Score[]; cancelled: boolean; error: string | null; usage: unknown; judge: string },
	): Promise<{ result: string; events: EngineEvent[]; summary: RankSummary; per_round: number | null }> {
		return this.call("POST", `/api/sessions/${encodeURIComponent(sid)}/rank/finish`, body);
	}
}
