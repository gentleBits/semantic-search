// Optional features: found in a folder (none is fine), with their routes, /api/me fields and page modules.
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { loadFeatures } from "../src/features.js";
import { Hub } from "../src/hub.js";
import { createApp } from "../src/server.js";

const hubIn = () => new Hub({ engineUrl: "http://127.0.0.1:9", stateDir: mkdtempSync(join(tmpdir(), "resumes-features-")), faux: true });
const ask = (app: ReturnType<typeof createApp>, path: string) => app.request(path, { headers: { host: "localhost:8765" } });

test("no folder, or an empty one: no features, and the server is as without them", async () => {
	const hub = hubIn();
	assert.deepEqual(await loadFeatures(hub, join(tmpdir(), "no-such-folder-here")), []);
	assert.deepEqual(await loadFeatures(hub, mkdtempSync(join(tmpdir(), "resumes-features-"))), []);
	const me = await (await ask(createApp(hub), "/api/me")).json();
	assert.deepEqual(me, { login: false, user: null });
	assert.equal((await ask(createApp(hub), "/api/demo")).status, 404);
});

test("a feature adds its route, its fields of /api/me, its page module and a note on the start line", async () => {
	const dir = mkdtempSync(join(tmpdir(), "resumes-features-"));
	writeFileSync(
		join(dir, "demo.js"),
		`export function feature(hub) {
			return {
				name: "demo",
				page: true,
				me: (user) => ({ demo: user === null }),
				routes(app, api) { app.get("/api/demo", (c) => api.answer(c, async () => ({ ok: true }))); },
				startLine: () => "demo on",
			};
		}`,
	);
	writeFileSync(join(dir, "notes.txt"), "not a module");
	const hub = hubIn();
	hub.features = await loadFeatures(hub, dir);
	assert.deepEqual(hub.features.map((f) => f.name), ["demo"]);
	assert.equal(hub.features[0].startLine?.(), "demo on");
	const app = createApp(hub);
	assert.deepEqual(await (await ask(app, "/api/me")).json(), { login: false, user: null, demo: true, features: ["demo"] });
	assert.deepEqual(await (await ask(app, "/api/demo")).json(), { ok: true });
});
