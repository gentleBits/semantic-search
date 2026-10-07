// Settings and providers over the API: model changes, server-side keys, the model lists.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { OPENAI_MODELS } from "../src/providers.js";
import { Api, type EngineHandle, FIXTURES, rankingsPage, startEngine, stubFetch } from "./helpers.js";

const FIXTURE = JSON.parse(readFileSync(join(FIXTURES, "openrouter-models.json"), "utf-8"));
let engine: EngineHandle | null = null;
before(async () => {
	engine = await startEngine();
});
after(() => engine?.stop());

const fetch = stubFetch({
	"https://openrouter.ai/api/v1/models": { status: 200, body: FIXTURE },
	// this week's top: the InclusionAI model is offered for being in it (not one of OPENROUTER_MAKERS), Qwen's for its maker
	"https://openrouter.ai/rankings": { status: 200, body: {}, text: rankingsPage(["stealth/space-bunny-alpha", "inclusionai/ling-3.0-flash-sante:free", "openai/gpt-6-luna"]) },
	"https://api.openai.com/v1/models": (h) =>
		h.Authorization === "Bearer sk-env-openai-key-0000"
			? { status: 200, body: { data: [{ id: "gpt-6-sol" }, { id: "gpt-6.1-sol" }, { id: "gpt-6-astra" }, { id: "gpt-6-luna" }, { id: "gpt-7-preview" }, { id: "gpt-5-mini" }, { id: "text-embedding-3-large" }, { id: "gpt-6-sol-realtime" }, { id: "whisper-1" }] } }
			: { status: 401, body: { error: { message: "Incorrect API key provided" } } },
	"https://openrouter.ai/api/v1/key": (h) =>
		h.Authorization === "Bearer sk-or-v1-abcdef0123456789" ? { status: 200, body: { data: { label: "resumes", limit: null, usage: 0.42, limit_remaining: null } } } : { status: 401, body: { error: { message: "User not found." } } },
});

test("the settings routes change the model without a restart; the keys are the server's; OpenRouter offers its free models", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const keys = { OPENAI_API_KEY: process.env.OPENAI_API_KEY, OPENROUTER_API_KEY: process.env.OPENROUTER_API_KEY };
	delete process.env.OPENROUTER_API_KEY;
	process.env.OPENAI_API_KEY = "sk-env-openai-key-0000";
	try {
		// a model newer than pi's registry on offer too (gpt-7-preview: OpenAI lists it, pi does not know it)
		const api = await new Api(engine, undefined, { fetch, model: "openai:gpt-5-mini", openaiModels: [...OPENAI_MODELS, "gpt-7-preview"] }).init();
		const s = (await api.request("GET", "/api/settings")).body;
		assert.deepEqual(s.chat, { provider: "openai", model: "gpt-5-mini", thinking: "medium", from: "config" }, "resumes.toml says medium");
		assert.equal(s.model, "openai:gpt-5-mini");
		assert.ok(!("keys" in s), "no key leaves the server, not even masked");
		assert.deepEqual(s.configured, ["openai"]);
		assert.deepEqual(s.providers.map((p: any) => [p.id, p.configured]), [["openrouter", false], ["openai", true]]);
		assert.ok(s.assistant === true && (await api.request("GET", "/api/config")).body.model === "openai:gpt-5-mini");

		let r = await api.request("PUT", "/api/settings", { keys: { openrouter: "sk-or-v1-abcdef0123456789" } });
		assert.ok(r.status === 422 && /keys are set on the server/.test(r.body.error.text), r.text);
		assert.equal((await api.request("POST", "/api/settings/test", { provider: "openrouter", key: "sk-or-v1-abcdef0123456789" })).status, 404);
		r = await api.request("PUT", "/api/settings", { chat: { provider: "openrouter", model: "qwen/qwen3.8-27b:free" } });
		assert.ok(r.status === 422 && r.body.error.text === "no key for openrouter on this server (OPENROUTER_API_KEY)", r.text);
		assert.equal((await api.request("GET", "/api/config")).body.model, "openai:gpt-5-mini", "a refused change changes nothing");

		// keys come only from the operator, through the settings file
		const file = join(api.stateDir, "settings.json");
		writeFileSync(file, JSON.stringify({ chat: {}, judge: {}, keys: { openrouter: "sk-or-v1-abcdef0123456789" } }));
		api.hub.settings.reload();
		assert.deepEqual((await api.request("GET", "/api/settings")).body.configured, ["openrouter", "openai"]);
		const models = (await api.request("GET", "/api/providers/openrouter/models")).body;
		assert.equal(models.source, "live · free models, this week's top first");
		assert.deepEqual(models.models.map((m: any) => m.id), ["inclusionai/ling-3.0-flash-sante:free", "qwen/qwen3.8-27b:free"], "the fixture's 17 models: two are free; the one in this week's top first");
		assert.ok(models.models.every((m: any) => m.in_per_m === 0 && m.out_per_m === 0));
		r = await api.request("PUT", "/api/settings", { chat: { provider: "openrouter", model: "anthropic/claude-sonnet-4.6", thinking: "medium" } });
		assert.ok(r.status === 422 && r.body.error.text === "anthropic/claude-sonnet-4.6 is not one of the 2 free models the server offers on OpenRouter", r.text);

		r = await api.request("PUT", "/api/settings", { chat: { provider: "openrouter", model: "qwen/qwen3.8-27b:free", thinking: "medium" } });
		assert.equal(r.status, 200, r.text);
		assert.ok(r.body.model === "openrouter:qwen/qwen3.8-27b:free" && r.body.assistant === true);
		const c = (await api.request("GET", "/api/config")).body;
		assert.ok(c.model === "openrouter:qwen/qwen3.8-27b:free" && c.judge === c.model && c.assistant === true, "the same running server");
		assert.deepEqual(JSON.parse(readFileSync(file, "utf-8")), {
			chat: { provider: "openrouter", model: "qwen/qwen3.8-27b:free", thinking: "medium" },
			judge: {},
			keys: { openrouter: "sk-or-v1-abcdef0123456789" },
		});

		const chat = await api.hub.chatModel();
		assert.ok(chat.key === "sk-or-v1-abcdef0123456789" && chat.thinking === "medium" && chat.model.api === "openai-completions" && chat.model.cost.input === 0 && chat.model.contextWindow === 262144);
		r = await api.request("PUT", "/api/settings", { judge: { provider: "openai", model: "gpt-6-luna", thinking: "off" } });
		assert.equal(r.status, 200, r.text);
		assert.equal((await api.request("GET", "/api/config")).body.judge, "openai:gpt-6-luna");
		const judge = await api.hub.judgeModel();
		assert.ok(judge.model.api === "openai-responses" && judge.model.provider === "openai" && judge.key === "sk-env-openai-key-0000" && judge.model.cost.input === 0.1 && judge.thinking === "off", "pi's registry knows the GPT-6 models; Luna takes off");

		// the offered models: the GPT-6 three from pi's registry, priced; one OpenAI lists that pi lacks has no price
		const openai = (await api.request("GET", "/api/providers/openai/models")).body;
		assert.equal(openai.source, "pi registry + OpenAI's list");
		assert.deepEqual(openai.models.map((m: any) => m.id), [...OPENAI_MODELS, "gpt-7-preview"], "only what is offered: not gpt-6-sol, gpt-5-mini or the non-chat ids");
		assert.ok(openai.models.some((m: any) => m.id === "gpt-6.1-sol" && m.reasoning && m.context === 272000 && m.in_per_m === 2 && m.levels.join() === "low,medium,high"));
		const seven = openai.models.find((m: any) => m.id === "gpt-7-preview");
		assert.ok(seven && seven.reasoning && seven.in_per_m === null && seven.context === 0, JSON.stringify(seven));
		r = await api.request("PUT", "/api/settings", { chat: { provider: "openai", model: "gpt-6-sol", thinking: "medium" } });
		assert.ok(r.status === 422 && /gpt-6-sol is not one of the 4 models the server offers on OpenAI/.test(r.body.error.text), r.text);
		r = await api.request("PUT", "/api/settings", { chat: { provider: "openai", model: "gpt-6.1-sol", thinking: "off" } });
		assert.equal(r.status, 200, r.text);
		const sol = await api.hub.chatModel();
		assert.ok(sol.model.id === "gpt-6.1-sol" && sol.model.cost.output === 10 && sol.thinking === "low", "off is not one of Sol's levels: it runs at low, said so");
		assert.equal((await api.request("PUT", "/api/settings", { chat: { provider: "openai", model: "gpt-7-preview", thinking: "medium" } })).status, 200);
		assert.equal((await api.request("GET", "/api/config")).body.model, "openai:gpt-7-preview");
		const seven5 = await api.hub.chatModel();
		assert.ok(seven5.model.id === "gpt-7-preview" && seven5.model.api === "openai-responses" && (seven5.model as any).unlisted && seven5.model.cost.input === 0 && seven5.key === "sk-env-openai-key-0000");
		r = await api.request("PUT", "/api/settings", { chat: { provider: "openai", model: "gpt-99-nope" } });
		assert.ok(r.status === 422 && /gpt-99-nope is not one of the \d+ models the server offers on OpenAI/.test(r.body.error.text), r.text);
		assert.equal((await api.request("GET", "/api/providers/anthropic/models")).status, 422);
		assert.equal((await api.request("PUT", "/api/settings", { chat: { provider: "openrouter", model: "gpt-5" } })).status, 422);
		assert.equal((await api.request("PUT", "/api/settings", '{"chat": {}}', { "content-type": "text/plain" })).status, 403, "own page only");
		assert.equal((await api.request("GET", "/api/settings")).body.chat.model, "gpt-7-preview", "a refused change changes nothing");
	} finally {
		for (const [k, v] of Object.entries(keys)) {
			if (v === undefined) delete process.env[k];
			else process.env[k] = v;
		}
	}
});

test("a turn is told the settings' model, and an unknown model is one line", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const dir = mkdtempSync(join(tmpdir(), "resumes-web-state-"));
	writeFileSync(join(dir, "settings.json"), JSON.stringify({ chat: {}, judge: {}, keys: { openrouter: "sk-or-v1-abcdef0123456789", openai: "sk-test-0000000000" } }));
	const api = await new Api(engine, dir, { fetch }).init();
	api.script = ["**3,026 people**."];
	let ev = await api.say("how many?");
	assert.equal(ev.at(-1)!.message.model, "faux:faux");
	assert.equal((await api.request("PUT", "/api/settings", { chat: { provider: "openrouter", model: "inclusionai/ling-3.0-flash-sante:free", thinking: "off" } })).status, 200);
	const chat = await api.hub.chatModel();
	assert.ok(chat.model.id === "inclusionai/ling-3.0-flash-sante:free" && chat.model.api === "openai-completions" && chat.key === "sk-or-v1-abcdef0123456789" && chat.thinking === "off");
	assert.equal((await api.request("PUT", "/api/settings", { chat: { provider: "openai", model: "gpt-99-nope" } })).status, 422, "not in pi's registry, and OpenAI's list answered 401 to the test key");
	writeFileSync(join(dir, "settings.json"), JSON.stringify({ chat: { provider: "openai", model: "gpt-99-nope" }, judge: {}, keys: { openai: "sk-test-0000000000" } }));
	api.hub.settings.reload();
	assert.equal((await api.request("GET", "/api/config")).body.assistant_off, "unknown model openai/gpt-99-nope", "written into the file by hand: refused when it would be called");
	ev = await api.say("hello");
	assert.equal(ev.at(-1)!.type, "error");
	assert.match(ev.at(-1)!.message.text, /^The assistant cannot be reached \(unknown model openai\/gpt-99-nope\)/);
});
