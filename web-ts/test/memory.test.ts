// The agent's memory (pi's transcript, compaction) and what the model is told about the data.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { Memory } from "../src/memory.js";
import { Api, type EngineHandle, kinds, last, SUMMARY, startEngine, told } from "./helpers.js";

let engine: EngineHandle | null = null;
before(async () => {
	engine = await startEngine();
});
after(() => engine?.stop());

const roles = (call: { messages?: any[] }) => call.messages!.map((m: any) => m.role);
const textOf = (m: any): string => (typeof m.content === "string" ? m.content : m.content.map((c: any) => (c.type === "text" ? c.text : "")).join(""));
const run = (name: string, fn: (api: Api) => Promise<void>, opts = {}) =>
	test(name, async (t) => {
		if (!engine) return t.skip("resumes engine or the index is missing");
		await fn(await new Api(engine!, undefined, opts).init());
	});

run("the agent remembers what it saw: across turns, and across a restart of the app server", async (api) => {
	api.script = [[["search", { topic: ["data pipelines"] }]], "**165 people**, mostly senior."];
	await api.say("who worked on data pipelines?");
	api.script = ["**165 people** — nothing changed."];
	await api.say("how many again?");
	const second = api.chatCalls.at(-1)!;
	assert.deepEqual(roles(second), ["user", "assistant", "toolResult", "assistant", "user"], "the whole first turn is in front of the model, its tool result included");
	const result = textOf(second.messages!.find((m: any) => m.role === "toolResult"));
	assert.ok(result.startsWith("165 people (before: 3,026)") && result.includes("country or state (the last part of the location): Germany 14"), result.slice(0, 200));
	assert.ok(told(second).startsWith("[screen] 165 people") && told(second).endsWith("how many again?"));

	const entries = api.piEntries();
	assert.equal(entries[0].type, "session", "pi's own session file, as the terminal pi writes it");
	assert.equal(entries.filter((e) => e.type === "message").length, 6, "user, the call, its result, the answer; user, the answer");
	assert.ok(entries.some((e) => e.type === "model_change" && e.provider === "faux" && e.modelId === "faux"));

	// a new Api on the same state dir and sid is a restart of the app server
	const again = await new Api(engine!, api.stateDir, { sid: api.sid }).init();
	again.script = ["**165 people**, as before."];
	const ev = await again.say("still the same?");
	assert.equal(last(ev, "done").message.text, "**165 people**, as before.");
	const first = again.chatCalls[0];
	assert.deepEqual(roles(first), ["user", "assistant", "toolResult", "assistant", "user", "assistant", "user"]);
	assert.ok(textOf(first.messages![0]).endsWith("who worked on data pipelines?"));
	assert.ok(!kinds(ev).includes("memory"), "nothing to compact");
});

run("a conversation from before the memory starts from its chat", async (api) => {
	await api.act({ type: "search", args: { skill: ["elixir"] } });
	const e = api.hub.engine;
	await e.message(api.sid, { role: "user", text: "who knows elixir?" });
	await e.message(api.sid, { role: "assistant", text: "**56 people** know Elixir.", steps: [{ sign: "?", text: "added skill: Elixir", detail: "3,026 → 56" }] });
	api.script = ["**56 people** — as I said."];
	await api.say("and how many was that?");
	const call = api.chatCalls[0];
	assert.deepEqual(roles(call), ["user", "assistant", "user"]);
	assert.equal(textOf(call.messages![0]), "[panel] the user added skill: Elixir → 56 people\nwho knows elixir?", "what the user did on the panel before asking is part of the seed");
	assert.equal(textOf(call.messages![1]), "**56 people** know Elixir.\n(did: added skill: Elixir 3,026 → 56)");
	assert.equal(api.piEntries().filter((x) => x.type === "message").length, 4, "the seed is in the transcript too");
});

run(
	"pi compacts the transcript when it outgrows the window; the next turn starts from the summary",
	async (api) => {
		api.script = [[["search", { topic: ["data pipelines"] }]], "**165 people**, mostly senior."];
		await api.say("who worked on data pipelines?");
		let compactedAt = 0;
		for (let i = 1; i <= 12 && !compactedAt; i++) {
			api.script = [[["overview", {}]], `**165 people**, look ${i}.`];
			const ev = await api.say(`what are they like, take ${i}?`);
			if (kinds(ev).includes("memory")) {
				compactedAt = i;
				const m = last(ev, "memory");
				assert.ok(m.compacted.tokens_before > 2000 && m.compacted.summary_chars === SUMMARY.length, JSON.stringify(m));
			}
		}
		assert.ok(compactedAt >= 2, `compacted after turn ${compactedAt}`);
		assert.equal(api.summaryCalls.length, 1, "one model call for the summary");
		assert.ok(api.summaryCalls[0].summary!.includes("<conversation>") && api.summaryCalls[0].summary!.includes("who worked on data pipelines?"));
		const entries = api.piEntries();
		const c = entries.find((e) => e.type === "compaction");
		assert.ok(c && c.summary === SUMMARY && c.tokensBefore > 2000 && entries.some((e) => e.id === c.firstKeptEntryId));

		api.script = ["**165 people**, still."];
		await api.say("and now?");
		const call = api.chatCalls.at(-1)!;
		const head = textOf(call.messages![0]);
		assert.ok(call.messages![0].role === "user" && head.includes("compacted into the following summary") && head.includes(SUMMARY), head.slice(0, 120));
		assert.ok(call.messages!.length < 6 * compactedAt, "the older turns are gone from what the model reads");
		assert.ok(told(call).endsWith("and now?"));
	},
	{ memoryWindow: 3000 },
);

run("a location filter takes several places; a place that matches nobody tells the model what the set holds", async (api) => {
	await api.act({ type: "search", args: { topic: ["data pipelines"] } });
	api.script = [[["filter", { location: ["Poland", "Spain", "Germany"], said: "in Poland, Spain or Germany" }]], "**32 people** — list updated."];
	let ev = await api.say("in Poland, Spain or Germany");
	const s = last(ev, "done").state;
	assert.equal(s.set.count, 32);
	assert.deepEqual([s.filters[1].kind, s.filters[1].value, s.filters[1].said], ["location", "Poland or Spain or Germany", "in Poland, Spain or Germany"]);
	assert.deepEqual(last(ev, "done").message.steps, [{ sign: "+", text: "added location: Poland or Spain or Germany", detail: "165 → 32" }]);
	assert.equal((await api.act({ type: "drop", targets: ["location"] })).body.state.set.count, 165);

	api.script = [[["filter", { location: ["EU"] }]], "No location text says “EU”: the 165 are in Germany, France, Spain, the Netherlands and so on. Shall I take those countries?"];
	ev = await api.say("the ones in the EU");
	const t = told(api.chatCalls.at(-1)!);
	assert.ok(t.includes("nobody is left: location: EU matched nobody of the 165 the other filters leave"), t);
	assert.ok(t.includes("the 165 people without it:") && t.includes("  where: location stated for 121 of 165 · open to remote 74"), t);
	assert.ok(t.includes("  country or state (the last part of the location): Germany 14 · France 11 · Spain 11 · Netherlands 10"), t);
	assert.equal(last(ev, "done").message.actions[0].label, "Remove location: EU");

	// the system prompt describes the data
	const first = api.chatCalls[0];
	assert.ok(first.tools!.length === 11 && first.system!.includes("3,026 resumes"));
	assert.ok(first.system!.includes("location: free text as the resume gives it, stated by 709 of 3,026 — most often “City, Country” (Krakow, Poland · Madrid, Spain · Hamburg, Germany)"), first.system);
	assert.ok(first.system!.includes("rate per hour in EUR, stated by 390 and estimated for 2,636") && first.system!.includes("years of experience, known for 2,965"));
	assert.ok(!first.system!.includes("→ filter") && !first.system!.includes("→ search"), "no phrase → tool rules");
});

test("a stopped or failed answer keeps its words in the transcript, never a call that did not run", () => {
	const sm = SessionManager.inMemory();
	const memory = new Memory("/nowhere");
	const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
	const base = { role: "assistant" as const, api: "faux", provider: "faux", model: "faux", usage, timestamp: 1 };
	memory.record(sm, { role: "user", content: "hello", timestamp: 1 });
	memory.record(sm, { ...base, content: [{ type: "text", text: "Let me look." }, { type: "toolCall", id: "c1", name: "overview", arguments: {} }], stopReason: "aborted" });
	memory.record(sm, { ...base, content: [], stopReason: "error", errorMessage: "HTTP 500" });
	const messages = sm.buildSessionContext().messages;
	assert.deepEqual(
		messages.map((m: any) => [m.role, m.stopReason, typeof m.content === "string" ? m.content : m.content.length]),
		[
			["user", undefined, "hello"],
			["assistant", "stop", 1],
		],
	);
});
