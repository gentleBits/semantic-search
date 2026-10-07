// Unit tests that need no engine and no model.
import assert from "node:assert/strict";
import { unscreened } from "../src/text.js";
import { mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { ResumesError } from "../src/errors.js";
import { Models, guardedStream } from "../src/models.js";
import { Catalog, OPENAI_MODELS, OPENROUTER_MAX, THINKING_LEVELS, chatModelId, isFree, openaiModel, openaiOffered, openaiRows, openaiVersion, openrouterModel, openrouterRows, perMillion, pickFree, registryRows, sizeOf, topIds, unlistedModel } from "../src/providers.js";
import { SUBMIT_SCORES, readAnswer, readScores } from "../src/rank.js";
import { Settings } from "../src/settings.js";
import { historyMessages, leadingCount, looksLikeJd, once, thinkingLevel } from "../src/text.js";
import { Auth, LOCK_AFTER, TTL_SECONDS, hashPassword, verifyPassword } from "../src/auth.js";
import { Owners } from "../src/owners.js";
import { Hono } from "hono";
import { LOCAL_HOSTS, ownPageOnly } from "../src/server.js";
import { FIXTURES, rankingsPage, stubFetch } from "./helpers.js";

// written by `resumes users add` (Python's hashlib.scrypt), salt 00 01 … 0f, password "cretzuel"
const PYTHON_HASH = "scrypt$14$8$1$AAECAwQFBgcICQoLDA0ODw$79MuiuHQMHk6N9iL1_is2-Bl4yCPwYO0SwBG-PNDijs";

const FIXTURE = JSON.parse(readFileSync(join(FIXTURES, "openrouter-models.json"), "utf-8"));

test("the readers of the turn", () => {
	assert.deepEqual([null, "", "none", "low", "HIGH", "banana", "off"].map(thinkingLevel), ["off", "off", "off", "low", "high", "low", "off"]);
	assert.equal(leadingCount("**3,026 people** — everyone."), 3026);
	assert.equal(leadingCount("**No one left.**"), null);
	assert.equal(leadingCount("#2 leads the **25 people**."), null, "only the count an answer opens with");
	assert.equal(once("**43 people**, ranked.\n**43 people**, ranked."), "**43 people**, ranked.");
	assert.equal(once("One.\n\nTwo.\nTwo.\nOne."), "One.\n\nTwo.\nOne.");
	assert.ok(looksLikeJd(`Senior Java Developer\n${"- a requirement line\n".repeat(30)}`) && !looksLikeJd("who worked on data pipelines?"));
});

test("the conversation in short, with what the user did on the panel", () => {
	const chat = [
		{ role: "user", text: "who worked on data pipelines?" },
		{ role: "assistant", text: "**165 people**.", steps: [{ sign: "?", text: "added topic: Data pipelines", detail: "3,026 → 165" }] },
		{ role: "event", kind: "drop", text: "you removed skill: Elixir → 165 people" },
		{ role: "event", kind: "page", text: "page 2 of 17 · 11–20" },
		{ role: "user", text: "/page 3", mono: true },
		{ role: "error", text: "No filter named “kotlin”.\nActive: Data pipelines." },
		{ role: "user", text: "", attachment: { file: "jd-1.md", name: "Java", lines: 9, chars: 500, preview: "…" } },
		{ role: "assistant", text: "" },
		{ role: "info", text: "f1 · topic: Data pipelines" },
	];
	const { history, panel } = historyMessages(chat as any);
	assert.deepEqual(history, [
		{ role: "user", content: "who worked on data pipelines?" },
		{ role: "assistant", content: "**165 people**.\n(did: added topic: Data pipelines 3,026 → 165)" },
		{ role: "user", content: "[panel] the user removed skill: Elixir → 165 people; the user typed the command /page 3; error shown: No filter named “kotlin”. | Active: Data pipelines.\n[attached job description, saved as jd-1.md]" },
		{ role: "assistant", content: "(no answer)" },
	]);
	assert.equal(panel, "[panel] the screen answered: f1 · topic: Data pipelines\n");
	const long = Array.from({ length: 40 }, (_, i) => [{ role: "user", text: `q${i}` }, { role: "assistant", text: `a${i}` }]).flat();
	const kept = historyMessages(long as any).history;
	assert.equal(kept.length, 28);
	assert.equal(kept[0].role, "user");
	assert.equal(historyMessages([{ role: "user", text: "x".repeat(900) }] as any).history[0].content.length, 702);
});

test("the judge's answer is read from the tool call, the text, or a bare array", () => {
	assert.deepEqual(readAnswer([{ type: "toolCall", id: "1", name: "submit_scores", arguments: { scores: [] } }]), { scores: [] });
	assert.deepEqual(readAnswer([{ type: "text", text: 'Sure: {"scores": [1]} done' }]), { scores: [1] });
	assert.deepEqual(readAnswer([{ type: "text", text: '```json\n{"scores": [2]}\n```' }]), { scores: [2] });
	assert.deepEqual(readAnswer([{ type: "text", text: "no json here" }]), {});
	assert.deepEqual(readAnswer([]), {});
	const bare = '[\n {"id": "r000001", "score": 88, "note": "strong"},\n {"id": "r000002", "score": 45, "note": "weak"}\n]';
	assert.deepEqual(readAnswer([{ type: "text", text: bare }]), { scores: JSON.parse(bare) }, "a bare array, as gpt-5-mini answered live");
	assert.equal((readAnswer([{ type: "text", text: `Here you go:\n${bare}\nDone.` }]).scores as any)[1].score, 45);
	const batch = [{ doc: 1, id: "r000001", card: "" }, { doc: 2, id: "r000002", card: "" }];
	const scores = readScores(batch, { scores: [{ id: "r000001", score: 140, note: `  a ${"b".repeat(200)}` }, { id: "r000002", score: "x" }, { id: "r000009", score: 1, note: "" }] }, 120);
	assert.equal(scores.length, 1);
	assert.equal(scores[0].score, 100);
	assert.equal(scores[0].note.length, 120);
	assert.ok(scores[0].note.endsWith("…"));
	assert.equal(SUBMIT_SCORES.name, "submit_scores");
	assert.deepEqual((SUBMIT_SCORES.parameters as any).required, ["scores"]);
	assert.equal(fauxAssistantMessage("x").role, "assistant");
});

test("only a free OpenRouter model is offered", () => {
	const rows = openrouterRows(JSON.parse(readFileSync(join(FIXTURES, "openrouter-models.json"), "utf-8")));
	assert.ok(rows.length > 2 && rows.filter(isFree).length === 2);
	assert.deepEqual(rows.filter(isFree).map((r) => r.id), ["inclusionai/ling-3.0-flash-sante:free", "qwen/qwen3.8-27b:free"]);
	assert.ok(rows.filter(isFree).every((r) => r.in_per_m === 0 && r.out_per_m === 0 && r.context === 262144));
});

test("settings: the file round trip, the server's keys, precedence over the environment, and what is refused", async () => {
	const dir = mkdtempSync(join(tmpdir(), "resumes-settings-"));
	const d = { model: "openai:gpt-5-mini", effort: "low", judge_model: null, judge_effort: "low" };
	const env = { OPENAI_API_KEY: process.env.OPENAI_API_KEY, OPENROUTER_API_KEY: process.env.OPENROUTER_API_KEY };
	delete process.env.OPENROUTER_API_KEY;
	process.env.OPENAI_API_KEY = "sk-env-openai-key-0000";
	try {
		const s = Settings.in(dir);
		assert.equal(s.path, join(dir, "settings.json"));
		assert.deepEqual(s.chatChoice(d), { provider: "openai", model: "gpt-5-mini", thinking: "low", from: "config" });
		assert.equal(s.judgeChoice(d).from, "chat");
		assert.equal(await s.key("openai"), "sk-env-openai-key-0000");
		assert.equal(s.keySource("openai"), "env");
		assert.equal(await s.key("openrouter"), undefined);
		assert.deepEqual(await s.configured(), ["openai"]);

		s.apply({ chat: { provider: "openrouter", model: "openai/gpt-5-mini", thinking: "medium" } });
		assert.equal(statSync(s.path).mode & 0o777, 0o600);
		const again = Settings.in(dir);
		assert.deepEqual(again.chatChoice(d), { provider: "openrouter", model: "openai/gpt-5-mini", thinking: "medium", from: "settings" });
		assert.deepEqual(again.judgeChoice(d), { provider: "openrouter", model: "openai/gpt-5-mini", thinking: "low", from: "chat" }, "the judge follows the chat unless set");
		const view: any = await again.view(d);
		assert.ok(!("keys" in view), "no key leaves the server, not even masked");
		assert.deepEqual(view.configured, ["openai"]);
		assert.deepEqual(view.providers.map((p: any) => [p.id, p.configured]), [["openrouter", false], ["openai", true]]);
		assert.equal(view.judge.same, true);
		assert.deepEqual(JSON.parse(readFileSync(s.path, "utf-8")), { chat: { provider: "openrouter", model: "openai/gpt-5-mini", thinking: "medium" }, judge: {}, keys: {} });

		// a key in the file wins over the environment, as in the terminal pi
		writeFileSync(s.path, JSON.stringify({ ...JSON.parse(readFileSync(s.path, "utf-8")), keys: { openrouter: "sk-or-v1-abcdef0123456789" } }));
		again.reload();
		assert.equal(await again.key("openrouter"), "sk-or-v1-abcdef0123456789");
		assert.equal(again.keySource("openrouter"), "settings");
		assert.deepEqual(await again.configured(), ["openrouter", "openai"]);
		again.apply({ judge: { provider: "openai", model: "gpt-5", thinking: "high" } });
		assert.deepEqual(again.judgeChoice(d), { provider: "openai", model: "gpt-5", thinking: "high", from: "settings" });
		assert.equal(JSON.parse(readFileSync(s.path, "utf-8")).keys.openrouter, "sk-or-v1-abcdef0123456789", "a choice is written next to the operator's key, which stays");
		again.apply({ judge: { same: true } });
		assert.equal(again.judgeChoice(d).from, "chat");
		again.apply({ chat: { thinking: "off" } });
		assert.deepEqual(again.chatChoice(d), { provider: "openai", model: "gpt-5-mini", thinking: "off", from: "config" }, "thinking alone keeps the config's model");
		for (const [bad, words] of [
			[{ chat: { provider: "nope", model: "x" } }, /no provider/],
			[{ chat: { provider: "openrouter", model: "gpt-5" } }, /vendor\/model/],
			[{ chat: { provider: "openai", model: "" } }, /missing/],
			[{ chat: { thinking: "max" } }, /thinking is one of/],
			[{ keys: { openai: "sk-typed-by-a-user" } }, /keys are set on the server/],
			[{ keys: ["x"] }, /keys are set on the server/],
			[{ chat: "x" }, /chat is an object/],
		] as [unknown, RegExp][]) {
			assert.throws(() => again.apply(bad), (e: any) => e instanceof ResumesError && words.test(e.message), JSON.stringify(bad));
		}
	} finally {
		for (const [k, v] of Object.entries(env)) {
			if (v === undefined) delete process.env[k];
			else process.env[k] = v;
		}
	}
});

test("OpenRouter rows and pi's registry rows", () => {
	const rows = openrouterRows(FIXTURE);
	const ids = rows.map((r) => r.id);
	assert.ok(ids.includes("openai/gpt-5-mini") && ids.includes("anthropic/claude-sonnet-4.6"));
	assert.ok(!ids.includes("typesafe/jev-router") && !ids.includes("inference-net/schematron-v2-turbo"), "models without tools are not offered");
	assert.deepEqual(ids, [...rows].sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase())).map((r) => r.id).filter((_, i, arr) => arr.length) && ids);
	const mini = rows.find((r) => r.id === "openai/gpt-5-mini")!;
	assert.deepEqual(mini, { id: "openai/gpt-5-mini", name: "OpenAI: GPT-5 Mini", context: 400000, max_tokens: 128000, in_per_m: 0.25, out_per_m: 2.0, cache_read_per_m: 0.025, reasoning: true, levels: THINKING_LEVELS, image: true, structured: true });
	const free = rows.find((r) => r.id.endsWith(":free"))!;
	assert.equal(free.in_per_m, 0);
	const m = openrouterModel(mini);
	assert.equal(m.api, "openai-completions");
	assert.equal(m.provider, "openrouter");
	assert.equal(m.baseUrl, "https://openrouter.ai/api/v1");
	assert.deepEqual(m.cost, { input: 0.25, output: 2.0, cacheRead: 0.025, cacheWrite: 0 });
	assert.deepEqual([m.contextWindow, m.maxTokens, m.input], [400000, 128000, ["text", "image"]]);
	const u = unlistedModel("vendor/new-model");
	assert.ok(u.unlisted && u.id === "vendor/new-model" && u.cost.input === 0);
	const reg = registryRows([{ id: "gpt-5-mini", name: "GPT-5 Mini", api: "openai-responses", provider: "openai", baseUrl: "", reasoning: true, input: ["text", "image"], cost: { input: 0.25, output: 2, cacheRead: 0.025, cacheWrite: 0 }, contextWindow: 400000, maxTokens: 128000 }]);
	assert.ok(reg[0].in_per_m === 0.25 && reg[0].image && reg[0].max_tokens === 128000);
	assert.equal(perMillion("0.00000025"), 0.25);
	assert.equal(perMillion(null), 0);
});

test("OpenRouter's free models: this week's top first, then the listed labs' sound ones, then OpenRouter's router; never a stealth model", () => {
	const row = (id: string, price = 0) => ({ id, name: id, context: 262144, max_tokens: 16384, in_per_m: price, out_per_m: price, cache_read_per_m: 0, reasoning: true, image: false, structured: true });
	// OpenRouter's free models that took tools when this test was written
	const today = ["apodex/apodex-1.1-mini:free", "cohere/north-mini-code:free", "dots-studio/dots-3-note-preview:free", "openrouter/free", "google/gemma-4-26b-a4b-it:free", "google/gemma-4-31b-it:free", "inclusionai/ling-3.0-flash-sante:free", "inclusionai/ling-3.1-flash", "liquid/lfm-2.5-2.6b:free", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "nvidia/nemotron-3-super-120b-a12b:free", "nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3.5-lightning:free", "poolside/laguna-s-2.1:free", "poolside/laguna-xs-2.1:free", "thinkingmachines/inkling:free", "thinkingmachines/inkling-small:free"].map((id) => row(id));
	const rows = [...today, row("deepseek/deepseek-v4.1-flash", 0.05), row("stealth/space-bunny-alpha")];
	const top = ["stealth/space-bunny-alpha", "deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash", "nvidia/nemotron-3-ultra-550b-a55b:free"];
	assert.deepEqual(pickFree(rows, top).map((r) => r.id), ["nvidia/nemotron-3-ultra-550b-a55b:free", "google/gemma-4-31b-it:free", "nvidia/nemotron-3-super-120b-a12b:free", "poolside/laguna-s-2.1:free", "thinkingmachines/inkling:free", "openrouter/free"]);
	assert.deepEqual(pickFree(rows).map((r) => r.id), ["google/gemma-4-31b-it:free", "nvidia/nemotron-3-super-120b-a12b:free", "nvidia/nemotron-3-ultra-550b-a55b:free", "poolside/laguna-s-2.1:free", "thinkingmachines/inkling:free", "openrouter/free"], "no top list: by name");
	assert.equal(pickFree(rows, ["inclusionai/ling-3.0-flash-sante:free"])[0].id, "inclusionai/ling-3.0-flash-sante:free", "a free model in the top list comes first, whoever makes it");
	const many = Array.from({ length: 12 }, (_, i) => row(`qwen/qwen-${30 + i}b:free`));
	const capped = pickFree([...many, row("openrouter/free")]);
	assert.ok(capped.length === OPENROUTER_MAX && capped.at(-1)!.id === "openrouter/free", "at most OPENROUTER_MAX, the router kept last");
	assert.deepEqual([sizeOf("nvidia/nemotron-3-ultra-550b-a55b:free"), sizeOf("liquid/lfm-2.5-2.6b:free"), sizeOf("thinkingmachines/inkling:free")], [{ total: 550, active: 55 }, { total: 2.6, active: null }, { total: null, active: null }]);
	// the leaderboard as the page carries it: in its JSON-LD, and escaped inside the page's script data
	const page = rankingsPage(["stealth/space-bunny-alpha", "nvidia/nemotron-3-ultra-550b-a55b:free"]);
	assert.deepEqual(topIds(page), ["stealth/space-bunny-alpha", "nvidia/nemotron-3-ultra-550b-a55b:free"]);
	assert.deepEqual(topIds(page.replace(/"/g, '\\"')), ["stealth/space-bunny-alpha", "nvidia/nemotron-3-ultra-550b-a55b:free"]);
	assert.deepEqual(topIds("<html>a page that changed</html>"), [], "no leaderboard: none");
});

test("a free OpenRouter model that refuses a one-word try gives its place to the next; a try is kept a week", async () => {
	const raw = (ids: string[]) => ({ data: ids.map((id) => ({ id, name: id, context_length: 262144, pricing: { prompt: "0", completion: "0" }, supported_parameters: ["tools", "reasoning"] })) });
	const tried: string[] = [];
	const fetch = stubFetch({
		"https://openrouter.ai/api/v1/models": { status: 200, body: raw(["thinkingmachines/inkling:free", "nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-31b-it:free", "openrouter/free"]) },
		"https://openrouter.ai/rankings": { status: 200, body: {}, text: rankingsPage(["thinkingmachines/inkling:free"]) },
		"https://openrouter.ai/api/v1/chat/completions": (h, body) => {
			tried.push(`${h.Authorization} ${body.model} ${body.max_tokens}`);
			if (body.model === "thinkingmachines/inkling:free") return { status: 403, body: { error: { message: "only available on agentic harnesses" } } };
			if (body.model === "google/gemma-4-31b-it:free") return { status: 429, body: { error: { message: "rate-limited upstream" } } };
			return { status: 200, body: {} };
		},
	});
	const dir = mkdtempSync(join(tmpdir(), "resumes-tries-"));
	const cat = fetching(dir, fetch, () => [], async (p) => (p === "openrouter" ? "sk-or-v1-abcdef0123456789" : undefined));
	const got = await cat.list("openrouter");
	assert.deepEqual(got.models.map((m) => m.id), ["google/gemma-4-31b-it:free", "nvidia/nemotron-3-super-120b-a12b:free", "openrouter/free"], "Inkling refused (though in the top list); Gemma only busy: kept");
	assert.deepEqual(tried.sort(), ["Bearer sk-or-v1-abcdef0123456789 google/gemma-4-31b-it:free 1", "Bearer sk-or-v1-abcdef0123456789 nvidia/nemotron-3-super-120b-a12b:free 1", "Bearer sk-or-v1-abcdef0123456789 openrouter/free 1", "Bearer sk-or-v1-abcdef0123456789 thinkingmachines/inkling:free 1"]);
	tried.length = 0;
	await cat.list("openrouter", { refresh: true });
	assert.deepEqual(tried, ["Bearer sk-or-v1-abcdef0123456789 google/gemma-4-31b-it:free 1"], "a refresh tries only the busy one again");
	const again = fetching(dir, fetch, () => [], async () => "sk-or-v1-abcdef0123456789");
	tried.length = 0;
	assert.ok(!(await again.list("openrouter")).models.some((m) => m.id === "thinkingmachines/inkling:free") && !tried.some((t) => t.includes("inkling")), "a restart remembers the refusal (on disk), without trying again");
	const keyless = fetching(null, fetch, () => []);
	tried.length = 0;
	assert.ok((await keyless.list("openrouter")).models.some((m) => m.id === "thinkingmachines/inkling:free") && !tried.length, "no key on the server: nothing tried, nothing dropped");
});

test("the catalog caches in memory and on disk, and works offline", async () => {
	const dir = mkdtempSync(join(tmpdir(), "resumes-catalog-"));
	const hits: number[] = [];
	const live = stubFetch({ "https://openrouter.ai/api/v1/models": () => (hits.push(1), { status: 200, body: FIXTURE }) });
	const cat = fetching(join(dir, "cache"), live);
	const first = await cat.list("openrouter");
	assert.equal(first.source, "live · free models");
	assert.deepEqual(first.models.map((m) => m.id), ["qwen/qwen3.8-27b:free"], `the fixture's ${openrouterRows(FIXTURE).length} tool-taking models: two are free; no top list (no rankings page), so only the one of a listed lab`);
	assert.ok(first.fetched_at && hits.length === 1);
	assert.equal((await cat.list("openrouter")).source, "live · free models");
	assert.equal(hits.length, 1, "an hour in memory");
	assert.equal((await cat.list("openrouter", { refresh: true })).source, "live · free models");
	assert.equal(hits.length, 2);
	assert.ok(statSync(join(dir, "cache", "openrouter-models.json")).isFile());
	assert.equal((await cat.openrouter("qwen/qwen3.8-27b:free")).cost.input, 0);
	assert.ok(((await cat.openrouter("openai/gpt-5-mini")) as any).unlisted, "a paid model is not in the list the service offers");
	assert.ok(((await cat.openrouter("x/y")) as any).unlisted);

	const offline = fetching(join(dir, "cache"), stubFetch({}));
	const got = await offline.list("openrouter");
	assert.equal(got.source, "live · free models (offline)");
	assert.equal(got.models.length, first.models.length);
	assert.match(got.note!, /no network/);
	// an older cache also holds paid rows: they are not offered either
	const older = join(dir, "older");
	mkdirSync(older, { recursive: true });
	writeFileSync(join(older, "openrouter-models.json"), JSON.stringify({ provider: "openrouter", rows: openrouterRows(FIXTURE), fetched_at: "2026-09-30T10:07:00+00:00", source: "live" }));
	const stale = await fetching(older, stubFetch({})).list("openrouter");
	assert.ok(stale.models.length === 2 && stale.models.every((m) => m.in_per_m === 0) && stale.source === "live (offline)");
	const nothing = fetching(join(dir, "empty"), stubFetch({}));
	assert.deepEqual(await nothing.list("openrouter"), { models: [], fetched_at: null, source: null, note: "openrouter.ai: no network" });
	const bad = fetching(null, stubFetch({ "https://openrouter.ai/api/v1/models": { status: 500, body: {} } }));
	assert.equal((await bad.list("openrouter")).note, "openrouter.ai answered HTTP 500");
	const pi = new Models(Settings.in(mkdtempSync(join(tmpdir(), "resumes-pi-")))).all();
	const reg = fetching(null, stubFetch({}), () => pi);
	const r = await reg.list("openai");
	assert.equal(r.source, "pi registry");
	assert.deepEqual(r.models.map((m) => m.id), OPENAI_MODELS);
	await assert.rejects(cat.list("anthropic"), /no provider/);
});


/** A catalog that fetches a due list before it answers: the checks below read what it fetched. */
function fetching(...args: ConstructorParameters<typeof Catalog>): Catalog {
	const c = new Catalog(...args);
	c.blocking = true;
	return c;
}

test("the model list answers at once, whatever its age: memory, else disk, else what ships with the app; the fetch runs in the background", async () => {
	const raw = { data: ["nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-31b-it:free"].map((id) => ({ id, name: id, context_length: 262144, pricing: { prompt: "0", completion: "0" }, supported_parameters: ["tools"] })) };
	let release!: () => void;
	const gate = new Promise<void>((r) => (release = r));
	let fetches = 0;
	const slow: Parameters<typeof stubFetch>[0] = { "https://openrouter.ai/api/v1/models": () => (fetches++, { status: 200, body: raw }) };
	const fetch = async (url: string, h?: Record<string, string>, b?: unknown) => {
		await gate; // the network takes as long as it takes
		return stubFetch(slow)(url, h, b);
	};
	const dir = mkdtempSync(join(tmpdir(), "resumes-instant-"));
	const cat = new Catalog(join(dir, "cache"), fetch, () => []);
	let t0 = Date.now();
	const first = await cat.list("openrouter");
	assert.ok(Date.now() - t0 < 100 && first.source === "shipped with the app" && first.models.length >= 1, `${Date.now() - t0} ms ${first.source}`);
	assert.ok(cat.list("openrouter") instanceof Promise && (await cat.list("openrouter")).source === "shipped with the app", "asked again while the fetch is out: the same, at once");
	release();
	await cat.settled();
	assert.equal(fetches, 1, "one fetch for both asks");
	const live = await cat.list("openrouter");
	assert.deepEqual(live.models.map((m) => m.id), ["google/gemma-4-31b-it:free", "nvidia/nemotron-3-super-120b-a12b:free"]);
	// a restart: the list on disk, at once, however old; it is fetched again behind it
	const later = new Catalog(join(dir, "cache"), async () => new Promise(() => {}), () => []); // a network that never answers
	t0 = Date.now();
	const fromDisk = await later.list("openrouter");
	assert.ok(Date.now() - t0 < 100 && fromDisk.models.length === 2, `${Date.now() - t0} ms`);
	// OpenAI's list needs no network at all: pi's registry
	const pi = new Models(Settings.in(mkdtempSync(join(tmpdir(), "resumes-pi-")))).all();
	t0 = Date.now();
	const openai = await new Catalog(null, async () => new Promise(() => {}), () => pi).list("openai");
	assert.ok(Date.now() - t0 < 100 && openai.models.map((m) => m.id).join() === OPENAI_MODELS.join(), `${Date.now() - t0} ms`);
});

test("an answer that copies the screen line is shown without it", () => {
	assert.equal(unscreened("[screen] 165 people · page 1 of 17 · filters: f1 topic: Data pipelines\n\n**44 people** don't state a location."), "**44 people** don't state a location.");
	assert.equal(unscreened("[panel] the user removed skill: Elixir\n[screen] 165 people\n**165 people** — as before."), "**165 people** — as before.");
	assert.equal(unscreened("**25 people** — list updated."), "**25 people** — list updated.");
	assert.equal(unscreened(""), "");
});

test("OpenAI: the three GPT-6 models from pi's registry, with prices and their own thinking levels; an offered id pi lacks becomes a custom model", async () => {
	const pi = new Models(Settings.in(mkdtempSync(join(tmpdir(), "resumes-pi-")))).all();
	const live = ["gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-7-preview", "gpt-5-mini", "gpt-5.5-pro", "o3", "text-embedding-3-large", "gpt-6-sol-realtime", "whisper-1"];
	const rows = openaiRows(pi, live);
	assert.deepEqual(rows.map((r) => r.id), ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"], "the three, most capable first; nothing else OpenAI lists");
	assert.deepEqual(rows.map((r) => [r.name, r.in_per_m, r.out_per_m, r.context]), [["GPT-6 Astra", 10, 50, 272000], ["GPT-6.1 Sol", 2, 10, 272000], ["GPT-6 Luna", 0.1, 0.5, 272000]], "pi's prices");
	assert.deepEqual(rows.map((r) => r.levels), [["low", "medium", "high"], ["low", "medium", "high"], ["off", "low", "medium", "high"]], "Sol and Astra take no off and no minimal");
	assert.deepEqual(openaiRows(pi, null).map((r) => r.id), OPENAI_MODELS, "no live list: pi's registry has them all");
	// an offered id pi's registry does not know (a model newer than pi): no price, called as a custom id
	const allowed = [...OPENAI_MODELS, "gpt-7-preview"];
	const seven = openaiRows(pi, live, allowed).at(-1)!;
	assert.ok(seven.id === "gpt-7-preview" && seven.in_per_m === null && seven.reasoning && seven.context === 0, JSON.stringify(seven));
	assert.deepEqual(openaiRows(pi, null, allowed).map((r) => r.id), OPENAI_MODELS, "not in pi's registry, not listed live: not offered");
	assert.deepEqual(openaiVersion("gpt-5-mini-2025-08-07"), { generation: 5, snapshot: true, alias: false });
	assert.deepEqual(openaiVersion("o3-mini"), { generation: 4.3, snapshot: false, alias: false });
	assert.ok(openaiOffered("gpt-6.1-sol") && openaiOffered("o3-mini") && openaiOffered("gpt-5-codex") && !openaiOffered("gpt-4o-2024-11-20") && !openaiOffered("gpt-5.1-chat-latest") && !openaiOffered("gpt-3.5-turbo"));
	assert.ok(chatModelId("gpt-6.1-sol") && chatModelId("o3-mini") && !chatModelId("gpt-6-sol-realtime") && !chatModelId("sora-2") && !chatModelId("dall-e-3"));
	const custom = openaiModel("gpt-7-preview", pi);
	assert.ok(custom.id === "gpt-7-preview" && custom.api === "openai-responses" && custom.baseUrl === "https://api.openai.com/v1" && custom.unlisted);
	assert.deepEqual(custom.cost, { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, "no price known: not another model's");
	assert.ok(openaiModel("gpt-7-preview", []).api === "openai-responses", "an empty registry still gives a callable model");

	const calls: string[] = [];
	const fetch = stubFetch({ "https://api.openai.com/v1/models": (h) => (calls.push(h.Authorization), { status: 200, body: { data: [{ id: "gpt-7-preview" }, { id: "gpt-6-astra" }, { id: "whisper-1" }] } }) });
	const cat = fetching(null, fetch, () => pi, async (p) => (p === "openai" ? "sk-server-key" : undefined));
	cat.openaiModels = allowed;
	const got = await cat.list("openai");
	assert.ok(got.source === "pi registry + OpenAI's list" && got.models.map((r) => r.id).join() === [...OPENAI_MODELS, "gpt-7-preview"].join() && calls[0] === "Bearer sk-server-key", got.models.map((r) => r.id).join());
	assert.ok((await cat.openai("gpt-7-preview"))?.unlisted && (await cat.openai("gpt-99-nope")) === null);
	const keyless = fetching(null, fetch, () => pi);
	assert.ok((await keyless.list("openai")).source === "pi registry" && (await keyless.list("openai")).models.length === 3, "no key on the server: no live list");
	const down = fetching(null, stubFetch({}), () => pi, async () => "sk-server-key");
	const off = await down.list("openai");
	assert.ok(off.source === "pi registry" && off.models.length === 3 && /no network.*registry alone/.test(off.note!), off.note);
});

test("a model call that goes silent ends as an error after the limit; a stop stays a stop; a reply passes untouched", async () => {
	const models = new Models(Settings.in(mkdtempSync(join(tmpdir(), "resumes-guard-"))));
	const faux = models.enableFaux();
	const model = faux.getModel("faux") as any;
	const ctx = { messages: [{ role: "user" as const, content: "hi", timestamp: Date.now() }] };
	// answers after `ms`, or as aborted when the call's signal fires first (as a real provider does)
	const slow = (ms: number) => (_c: unknown, o: any) =>
		new Promise((res) => {
			const t = setTimeout(() => res(fauxAssistantMessage("late")), ms);
			o?.signal?.addEventListener("abort", () => (clearTimeout(t), res(fauxAssistantMessage([], { stopReason: "aborted", errorMessage: "Request was aborted" }))));
		});
	const stream = guardedStream(models.pi, 1000);
	faux.setResponses([slow(5000) as any]);
	let t0 = Date.now();
	const idle = await stream(model, ctx).result();
	assert.ok(idle.stopReason === "error" && idle.errorMessage === "no reply from the model for 1 s" && Date.now() - t0 < 3000, `${idle.stopReason} ${idle.errorMessage} ${Date.now() - t0} ms`);
	const ac = new AbortController();
	setTimeout(() => ac.abort(), 100);
	faux.setResponses([slow(5000) as any]);
	t0 = Date.now();
	const stopped = await stream(model, ctx, { signal: ac.signal }).result();
	assert.ok(stopped.stopReason === "aborted" && Date.now() - t0 < 900, `${stopped.stopReason} ${Date.now() - t0} ms`);
	faux.setResponses([fauxAssistantMessage("right away")]);
	const ok = await stream(model, ctx).result();
	assert.ok(ok.stopReason === "stop" && ok.content.some((c: any) => c.type === "text" && c.text === "right away"));
});

test("the login's pieces: the hash Python writes, the cookie's token, the lock, the owners file", () => {
	assert.equal(hashPassword("cretzuel", { salt: Buffer.from([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]) }), PYTHON_HASH, "the same bytes as hashlib.scrypt");
	assert.ok(verifyPassword("cretzuel", PYTHON_HASH) && !verifyPassword("cretzuel ", PYTHON_HASH) && !verifyPassword("", PYTHON_HASH));
	const h = hashPassword("another one");
	assert.ok(h.startsWith("scrypt$14$8$1$") && h !== hashPassword("another one"), "a fresh salt each time");
	// change the second-to-last character: the last of 43 carries only 4 bits, so two letters can decode alike
	const changed = `${h.slice(0, -2)}${h.at(-2) === "A" ? "B" : "A"}${h.at(-1)}`;
	assert.ok(verifyPassword("another one", h) && !verifyPassword("another one", changed));
	assert.ok(!verifyPassword("x", "") && !verifyPassword("x", "bcrypt$1$2$3$4$5") && !verifyPassword("x", "scrypt$99$8$1$AA$AA"), "a broken record is a wrong password, not a crash");

	const dir = mkdtempSync(join(tmpdir(), "resumes-auth-"));
	let now = 1_800_000_000_000;
	const auth = new Auth(dir, () => now);
	assert.equal(auth.on(), false, "no file: login off");
	writeFileSync(join(dir, "users.json"), JSON.stringify({ users: { "Dana@Example.com": { hash: PYTHON_HASH }, broken: { hash: "md5$x" }, "": { hash: PYTHON_HASH } } }));
	assert.ok(auth.on() && auth.users.count() === 1 && auth.users.has("dana@example.com"), "a name is lowercased; a broken or empty one is skipped");
	assert.ok(auth.users.verify("DANA@example.com", "cretzuel") && !auth.users.verify("dana@example.com", "wrong") && !auth.users.verify("nobody", "cretzuel"));
	const token = auth.token("dana@example.com");
	assert.match(token, /^[A-Za-z0-9_-]+\.\d+\.[A-Za-z0-9_-]+$/);
	assert.equal(auth.read(token), "dana@example.com");
	assert.equal(statSync(join(dir, "secret")).mode & 0o777, 0o600);
	assert.equal(new Auth(dir, () => now).read(token), "dana@example.com", "the secret is the file's: a restart keeps everyone logged in");
	now += (TTL_SECONDS - 1) * 1000;
	assert.equal(auth.read(token), "dana@example.com");
	now += 2000;
	assert.equal(auth.read(token), null, "expired");
	now -= 10_000;
	assert.equal(auth.read(token.slice(0, -2) + "xx"), null, "a bad signature");
	assert.equal(auth.read(token.replace(/\.\d+\./, ".9999999999.")), null, "an expiry moved by hand");
	assert.equal(auth.read(undefined), null);
	assert.equal(auth.read("a.b"), null);
	writeFileSync(join(dir, "users.json"), JSON.stringify({ users: {} }));
	assert.equal(auth.read(token), null, "removed from the file: the cookie is worth nothing");
	assert.equal(auth.on(), false);

	assert.equal(auth.lockedFor("dana@example.com"), 0);
	assert.equal(LOCK_AFTER, 5);

	const owners = Owners.in(dir);
	assert.equal(owners.of("s1"), null);
	owners.set("s1", "dana@example.com");
	owners.set("s2", "cole@example.com");
	assert.equal(owners.of("s1"), "dana@example.com");
	assert.deepEqual([...Owners.in(dir).mine("dana@example.com")], ["s1"], "written to disk");
	owners.forget("s1");
	owners.forget("never");
	assert.deepEqual(Object.keys(JSON.parse(readFileSync(join(dir, "owners.json"), "utf-8"))), ["s2"]);
});

test("behind a proxy: the public name is accepted in Host and Origin, other names are not", async () => {
	const app = new Hono();
	app.use("*", ownPageOnly(new Set([...LOCAL_HOSTS, "search.example"])));
	app.get("/api/x", (c) => c.text("ok"));
	app.post("/api/x", (c) => c.text("posted"));
	const ask = (host: string, extra: Record<string, string> = {}, method = "GET") =>
		app.request("/api/x", { method, headers: { host, ...extra, ...(method === "POST" ? { "content-type": "application/json", "content-length": "2" } : {}) }, body: method === "POST" ? "{}" : undefined });
	assert.equal((await ask("search.example")).status, 200);
	assert.equal((await ask("Search.Example")).status, 200, "names are compared lowercased");
	assert.equal((await ask("search.example", { origin: "https://search.example" }, "POST")).status, 200, "the browser's Origin over https matches the Host Caddy passes through");
	assert.equal((await ask("localhost:8765", { origin: "http://localhost:8765" }, "POST")).status, 200);
	assert.equal((await ask("other.example")).status, 403);
	assert.equal((await ask("203.0.113.10")).status, 403, "the bare IP is not a name the server answers as");
	assert.equal((await ask("search.example", { origin: "https://evil.example" }, "POST")).status, 403);
	assert.equal((await ask("search.example", { origin: "https://search.example" }, "POST").then(async () => (await app.request("/api/x", { method: "POST", headers: { host: "search.example", origin: "https://search.example", "content-type": "text/plain", "content-length": "2" }, body: "{}" })).status)), 403, "a body must be JSON");
});
