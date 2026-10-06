// Usage limits and per-person model choice over the API: members' allowances, admins without one, the ledger.
import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { hashPassword } from "../src/auth.js";
import { inWords } from "../src/usage.js";
import { Api, type EngineHandle, FIXTURES, kinds, last, startEngine, stubFetch } from "./helpers.js";

let engine: EngineHandle | null = null;
before(async () => {
	engine = await startEngine();
});
after(() => engine?.stop());

const ADMIN = "dana@example.com";
const MEMBER = "ana@example.com";

function people(api: Api): void {
	writeFileSync(
		join(api.stateDir, "users.json"),
		JSON.stringify({
			users: {
				[ADMIN]: { hash: hashPassword("cretzuel"), created: "2026-09-30T18:00:00+00:00" },
				[MEMBER]: { hash: hashPassword("a long password"), created: "2026-10-01T10:00:00+00:00", role: "member", phone: "+40712345678", via: "signup" },
			},
		}),
	);
}

/** Logged in as `name`: the cookie on every request of this Api. */
async function as(api: Api, name: string, password: string): Promise<Api> {
	const r = await api.request("POST", "/api/login", { user: name, password });
	assert.equal(r.status, 200, r.text);
	api.headers = { cookie: r.headers.getSetCookie()[0].split(";")[0] };
	return api;
}

/** A turn → its events (the stream read whole). */
async function turn(api: Api, sid: string, text: string): Promise<Record<string, any>[]> {
	const r = await api.request("POST", `/api/sessions/${sid}/chat`, { text });
	assert.equal(r.status, 200, r.text);
	return r.text
		.split("\n\n")
		.map((b) => b.split("\n").find((l) => l.startsWith("data: ")))
		.filter(Boolean)
		.map((l) => JSON.parse(l!.slice(6)));
}

const ledger = (api: Api) =>
	existsSync(join(api.stateDir, "usage.jsonl"))
		? readFileSync(join(api.stateDir, "usage.jsonl"), "utf-8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l))
		: [];

test("a member's allowance: turns a day, dollars a day; the admin has none; every turn is in the ledger", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const api = new Api(engine, undefined, { policy: { limits: { turns_per_day: 2, usd_per_day: 1 } } });
	people(api);
	await as(api, MEMBER, "a long password");
	await api.init();
	api.script = ["first answer.", "second answer."];
	assert.equal(last(await turn(api, api.sid, "who knows Elixir?"), "done").message.text, "first answer.");
	assert.equal(last(await turn(api, api.sid, "and Kubernetes?"), "done").message.text, "second answer.");
	const ev = await turn(api, api.sid, "and Go?");
	assert.deepEqual(kinds(ev), ["error"], "refused before anything is said or spent");
	const err = last(ev, "error").message;
	assert.equal(err.code, "USAGE_LIMIT");
	assert.match(err.text, /You've used today's assistant allowance — it's back in (2[34] h|\d+ h \d+ min)\. The list, the filters and the slash commands keep working\./);
	assert.equal(api.chatCalls.length, 2, "the model was not called a third time");
	const lines = ledger(api);
	assert.deepEqual(lines.map((l) => [l.user, l.sid, l.model]), [[MEMBER, api.sid, "faux:faux"], [MEMBER, api.sid, "faux:faux"]]);
	assert.equal((await api.act({ type: "search", args: { topic: ["data pipelines"] } })).status, 200);
	const slash = await turn(api, api.sid, "/filters");
	assert.equal(last(slash, "done").type, "done");

	const admin = await as(new Api(engine, api.stateDir, { policy: { limits: { turns_per_day: 2, usd_per_day: 1 } } }), ADMIN, "cretzuel");
	await admin.init();
	admin.script = ["a", "b", "c"];
	for (const x of ["a", "b", "c"]) assert.equal(last(await turn(admin, admin.sid, `turn ${x}`), "done").message.text, x);
	assert.equal(ledger(admin).filter((l) => l.user === ADMIN).length, 3, "the admin's turns are counted too (resumes users usage)");

	const rich = new Api(engine, undefined, { policy: { limits: { turns_per_day: 100, usd_per_day: 1 } } });
	people(rich);
	await as(rich, MEMBER, "a long password");
	await rich.init();
	rich.hub.usage.record(MEMBER, rich.sid, { in: 1, out: 1, cached: 0, calls: 1, cost: 0.6 }, "openai:gpt-5");
	rich.script = ["fine."];
	assert.equal(last(await turn(rich, rich.sid, "one more"), "done").message.text, "fine.", "$0.60 spent: still within $1");
	rich.hub.usage.record(MEMBER, rich.sid, { in: 1, out: 1, cached: 0, calls: 1, cost: 0.45 }, "openai:gpt-5");
	assert.equal(last(await turn(rich, rich.sid, "and another"), "error").message.code, "USAGE_LIMIT", "$1.05: over");
	const settings = (await rich.request("GET", "/api/settings")).body;
	assert.deepEqual({ ...settings.allowance, cost: Math.round(settings.allowance.cost * 100) / 100 }, { turns: 3, cost: 1.05, turns_per_day: 100, usd_per_day: 1, sessions: 1, sessions_per_day: 10 });

	// on a restart the allowance is read back from the ledger
	const again = new Api(engine, rich.stateDir, { policy: { limits: { turns_per_day: 100, usd_per_day: 1 } } });
	await as(again, MEMBER, "a long password");
	assert.equal(last(await turn(again, rich.sid, "after a restart"), "error").message.code, "USAGE_LIMIT");
	assert.equal(inWords(3 * 3600_000 + 20 * 60_000), "3 h 20 min");
	assert.equal(inWords(59_000), "1 min");
});

test("one number, one allowance: addresses that came through the check with the same number share the day's turns, sessions and the one at a time", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const policy = { limits: { turns_per_day: 2, usd_per_day: 1 } };
	const api = new Api(engine, undefined, { policy });
	const member = (phone: string) => ({ hash: hashPassword("a long password"), created: "2026-10-06T10:00:00+00:00", role: "member", phone, phones: [phone], via: "check" });
	writeFileSync(join(api.stateDir, "users.json"), JSON.stringify({ users: { [MEMBER]: member("+40712345678"), "bob@example.com": member("+40712345678"), "cid@example.com": member("+40712345679") } }));
	// one server, three browsers: each person's cookie in turn
	const cookie: Record<string, Record<string, string>> = {};
	for (const name of [MEMBER, "bob@example.com", "cid@example.com"]) cookie[name] = (await as(api, name, "a long password")).headers;
	const sid: Record<string, string> = {};
	for (const name of Object.keys(cookie)) {
		api.headers = cookie[name];
		sid[name] = (await api.request("POST", "/api/sessions", {})).body.session.id;
	}
	const say = (name: string, text: string) => ((api.headers = cookie[name]), turn(api, sid[name], text));
	api.script = ["ana's answer.", "bob's answer.", "cid's answer."];
	assert.equal(last(await say(MEMBER, "who knows Elixir?"), "done").message.text, "ana's answer.");
	assert.equal(last(await say("bob@example.com", "and Go?"), "done").message.text, "bob's answer.");
	assert.equal(last(await say("bob@example.com", "and Rust?"), "error").message.code, "USAGE_LIMIT", "the number's 3rd turn of the day");
	assert.equal(last(await say(MEMBER, "and Rust?"), "error").message.code, "USAGE_LIMIT", "for ana too");
	api.headers = cookie[MEMBER];
	const settings = (await api.request("GET", "/api/settings")).body;
	assert.deepEqual([settings.allowance.turns, settings.allowance.sessions], [2, 2], "ana sees the number's day: two turns, two new sessions");
	assert.deepEqual(ledger(api).map((l) => l.user), [MEMBER, "bob@example.com"], "each turn is still its sender's");
	assert.equal(last(await say("cid@example.com", "hello"), "done").message.text, "cid's answer.", "another number, its own allowance");

	// one at a time, across the people of one number
	const busy = new Api(engine, undefined);
	writeFileSync(join(busy.stateDir, "users.json"), readFileSync(join(api.stateDir, "users.json"), "utf-8"));
	for (const name of [MEMBER, "bob@example.com"]) {
		cookie[name] = (await as(busy, name, "a long password")).headers;
		sid[name] = (await busy.request("POST", "/api/sessions", {})).body.session.id;
	}
	busy.script = [{ text: "slow answer.", delayMs: 1500 }, "later."];
	busy.headers = cookie[MEMBER];
	const slow = turn(busy, sid[MEMBER], "a slow question");
	await new Promise((ok) => setTimeout(ok, 300));
	busy.headers = cookie["bob@example.com"];
	const ev = await turn(busy, sid["bob@example.com"], "meanwhile, on bob's laptop");
	assert.equal(last(ev, "error").message.code, "BUSY");
	assert.equal(last(await slow, "done").message.text, "slow answer.");
});

test("one turn at a time per member, across their searches; an admin may run two", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const api = new Api(engine);
	people(api);
	await as(api, MEMBER, "a long password");
	await api.init();
	const other = (await api.request("POST", "/api/sessions", {})).body.session.id;
	api.script = [{ text: "slow answer.", delayMs: 1500 }, "the other one."];
	const slow = turn(api, api.sid, "a slow question");
	await new Promise((ok) => setTimeout(ok, 300));
	const ev = await turn(api, other, "meanwhile, in another tab");
	assert.equal(last(ev, "error").message.code, "BUSY");
	assert.match(last(ev, "error").message.text, /still working on another search/);
	assert.equal(last(await slow, "done").message.text, "slow answer.");
	assert.equal(last(await turn(api, other, "now?"), "done").message.text, "the other one.", "free again once the first is done");

	const admin = await as(new Api(engine, api.stateDir), ADMIN, "cretzuel");
	await admin.init();
	const second = (await admin.request("POST", "/api/sessions", {})).body.session.id;
	admin.script = [{ text: "one.", delayMs: 800 }, "two."];
	const [a, b] = await Promise.all([turn(admin, admin.sid, "first"), (async () => {
		await new Promise((ok) => setTimeout(ok, 200));
		return turn(admin, second, "second");
	})()]);
	assert.deepEqual([last(a, "done").message.text, last(b, "done").message.text].sort(), ["one.", "two."]);
});

test("the model is each person's: a turn runs on its sender's pick; a member picks only a priced model", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const api = new Api(engine);
	people(api);
	writeFileSync(join(api.stateDir, "user-settings.json"), JSON.stringify({ [ADMIN]: { chat: { provider: "faux", model: "faux-thinker", thinking: "low" }, judge: {} } }));
	const admin = await as(new Api(engine, api.stateDir), ADMIN, "cretzuel");
	await admin.init();
	admin.script = ["from the admin's model."];
	assert.equal(last(await turn(admin, admin.sid, "hi"), "done").message.model, "faux:faux-thinker");
	assert.equal((await admin.request("GET", "/api/config")).body.model, "faux:faux-thinker");
	const member = await as(new Api(engine, api.stateDir), MEMBER, "a long password");
	await member.init();
	member.script = ["from the default."];
	assert.equal(last(await turn(member, member.sid, "hi"), "done").message.model, "faux:faux", "the admin's pick changed nothing for her");
	assert.equal((await member.request("GET", "/api/config")).body.model, "faux:faux");

	// gpt-5-mini has a price in pi's registry; gpt-6-sol has none
	const fetch = stubFetch({
		"https://openrouter.ai/api/v1/models": { status: 200, body: JSON.parse(readFileSync(join(FIXTURES, "openrouter-models.json"), "utf-8")) },
		"https://api.openai.com/v1/models": { status: 200, body: { data: [{ id: "gpt-6-sol" }, { id: "gpt-5-mini" }, { id: "gpt-5.5-pro" }] } },
	});
	const key = process.env.OPENAI_API_KEY;
	process.env.OPENAI_API_KEY = "sk-env-openai-key-0000";
	try {
		const a = await as(new Api(engine, api.stateDir, { fetch, model: "openai:gpt-5-mini" }), ADMIN, "cretzuel");
		const m = await as(new Api(engine, api.stateDir, { fetch, model: "openai:gpt-5-mini" }), MEMBER, "a long password");
		let s = (await m.request("GET", "/api/settings")).body;
		let r0: Awaited<ReturnType<Api["request"]>>;
		assert.ok(s.yours && s.priced_only && s.chat.from === "config", JSON.stringify(s));
		const listing = (await m.request("GET", "/api/providers/openai/models")).body;
		assert.ok(listing.priced_only && listing.models.some((x: any) => x.id === "gpt-5-mini") && !listing.models.some((x: any) => x.id === "gpt-6-sol"), "a member is not shown the unpriced");
		assert.ok(!listing.models.some((x: any) => x.out_per_m > 40) && listing.max_usd_per_m_out === 40, "nor the dearest");
		const pro = (await a.request("GET", "/api/providers/openai/models")).body.models.find((x: any) => x.out_per_m > 40);
		assert.ok(pro, "the registry has a model above $40/M out (a 'pro' one) for this check");
		r0 = await m.request("PUT", "/api/settings", { chat: { provider: "openai", model: pro.id } });
		assert.ok(r0.status === 422 && /the limit for your account is \$40/.test(r0.body.error.text), r0.text);
		r0 = await m.request("PUT", "/api/settings", { judge: { provider: "openai", model: pro.id } });
		assert.ok(r0.status === 422, "the ranking model too");
		assert.ok((await a.request("GET", "/api/providers/openai/models")).body.models.some((x: any) => x.id === "gpt-6-sol"), "an admin is");
		let r = await m.request("PUT", "/api/settings", { chat: { provider: "openai", model: "gpt-6-sol", thinking: "low" } });
		assert.ok(r.status === 422 && /no known price/.test(r.body.error.text), r.text);
		r = await m.request("PUT", "/api/settings", { chat: { provider: "openai", model: "gpt-5-mini", thinking: "high" } });
		assert.equal(r.status, 200, r.text);
		assert.deepEqual([r.body.chat.model, r.body.chat.thinking, r.body.chat.from], ["gpt-5-mini", "high", "yours"]);
		r = await a.request("PUT", "/api/settings", { chat: { provider: "openai", model: "gpt-6-sol", thinking: "medium" } });
		assert.equal(r.status, 200, "an admin may pick a model with no price");
		assert.equal((await a.request("GET", "/api/config")).body.model, "openai:gpt-6-sol");
		assert.equal((await m.request("GET", "/api/config")).body.model, "openai:gpt-5-mini", "each their own");
		const picks = JSON.parse(readFileSync(join(api.stateDir, "user-settings.json"), "utf-8"));
		assert.deepEqual(Object.keys(picks).sort(), [ADMIN, MEMBER].sort());
		assert.ok(!existsSync(join(api.stateDir, "settings.json")) || !JSON.parse(readFileSync(join(api.stateDir, "settings.json"), "utf-8")).chat?.model, "the server's default is untouched");
		r = await m.request("GET", "/api/providers/openai/models?refresh=1");
		assert.ok(r.status === 403 && r.body.error.code === "ADMIN_ONLY", r.text);
		assert.equal((await a.request("GET", "/api/providers/openai/models?refresh=1")).status, 200);
		s = (await a.request("GET", "/api/settings")).body;
		assert.ok(s.yours && !s.priced_only && !("allowance" in s));

		// with an unpriced server default, a member who never picked falls back to resumes.toml's model
		const fresh = new Api(engine, undefined, { fetch, model: "openai:gpt-5-mini" });
		people(fresh);
		writeFileSync(join(fresh.stateDir, "settings.json"), JSON.stringify({ chat: { provider: "openai", model: "gpt-6-sol", thinking: "medium" }, judge: {}, keys: {} }));
		fresh.hub.settings.reload();
		const mem = await as(new Api(engine, fresh.stateDir, { fetch, model: "openai:gpt-5-mini" }), MEMBER, "a long password");
		const adm = await as(new Api(engine, fresh.stateDir, { fetch, model: "openai:gpt-5-mini" }), ADMIN, "cretzuel");
		const cm = (await mem.request("GET", "/api/config")).body;
		assert.deepEqual([cm.model, cm.judge], ["openai:gpt-5-mini", "openai:gpt-5-mini"], "the member: a priced model");
		assert.equal((await mem.hub.chatModel(undefined, MEMBER)).name, "openai:gpt-5-mini", "and that is what a turn runs on");
		assert.equal((await adm.request("GET", "/api/config")).body.model, "openai:gpt-6-sol", "the admin: the server's default");
	} finally {
		if (key === undefined) delete process.env.OPENAI_API_KEY;
		else process.env.OPENAI_API_KEY = key;
	}
});

test("new sessions a day: a member's 4th is refused, deleting gives none back, one with none left may start one, the admin has no limit, a restart keeps the count", async (t) => {
	if (!engine) return t.skip("resumes engine or the index is missing");
	const clock = { now: Date.UTC(2026, 9, 2, 10, 0, 0) };
	const api = new Api(engine, undefined, { policy: { limits: { sessions_per_day: 3 } }, now: () => clock.now });
	people(api);
	await as(api, MEMBER, "a long password");
	const made: string[] = [];
	for (let i = 0; i < 3; i++) {
		const r = await api.request("POST", "/api/sessions", {});
		assert.equal(r.status, 201, r.text);
		made.push(r.body.session.id);
		clock.now += 60_000;
	}
	let r = await api.request("POST", "/api/sessions", {});
	assert.equal(r.status, 429, r.text);
	assert.equal(r.body.error.code, "SESSION_LIMIT");
	assert.equal(r.body.error.text, "You have reached the limit of 3 new sessions per day; you can start another in 23 h 57 min.");
	assert.equal((await api.request("GET", "/api/sessions")).body.sessions.length, 3, "nothing was made");
	assert.equal((await api.request("GET", `/api/sessions/${made[0]}`)).status, 200, "the existing sessions keep working");

	assert.equal((await api.request("DELETE", `/api/sessions/${made[0]}`)).status, 200);
	assert.equal((await api.request("POST", "/api/sessions", {})).status, 429, "deleting one gives none back");
	for (const sid of made.slice(1)) assert.equal((await api.request("DELETE", `/api/sessions/${sid}`)).status, 200);
	r = await api.request("POST", "/api/sessions", {});
	assert.equal(r.status, 201, "none left: the page may open a new one");
	assert.equal((await api.request("POST", "/api/sessions", {})).status, 429, "but only that one");

	const s = await api.request("GET", "/api/settings");
	assert.equal(s.body.allowance?.sessions, 4, s.text);
	assert.equal(s.body.allowance?.sessions_per_day, 3);

	const again = await as(new Api(engine, api.stateDir, { policy: { limits: { sessions_per_day: 3 } }, now: () => clock.now }), MEMBER, "a long password");
	assert.equal((await again.request("POST", "/api/sessions", {})).status, 429, "a restart resets nothing");
	clock.now += 24 * 3600_000;
	assert.equal((await again.request("POST", "/api/sessions", {})).status, 201, "a day later: one again");

	const admin = await as(new Api(engine, api.stateDir, { policy: { limits: { sessions_per_day: 3 } }, now: () => clock.now }), ADMIN, "cretzuel");
	for (let i = 0; i < 5; i++) assert.equal((await admin.request("POST", "/api/sessions", {})).status, 201, "the admin: no limit");
});
