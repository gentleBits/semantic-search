/**
 * Optional parts of the app: every module in features/ is loaded at start, and none is fine. A feature may add
 * routes, fields of /api/me, a page module (public/features/<name>.js) and a note on the start line.
 */
import { readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import type { Context, Hono } from "hono";
import type { Hub } from "./hub.js";

export interface FeatureApi {
	answer(c: Context, fn: () => Promise<unknown>): Promise<Response>;
	bodyOf(c: Context): Promise<Record<string, unknown>>;
	user(c: Context): string; // the logged-in person, else a 401
}

export interface Feature {
	name: string;
	page?: boolean; // the page loads public/features/<name>.js
	routes?(app: Hono<any>, api: FeatureApi): void;
	me?(user: string | null): Record<string, unknown>;
	startLine?(): string;
}

export type FeatureFactory = (hub: Hub) => Feature;

const DIR = join(dirname(fileURLToPath(import.meta.url)), "features");

export async function loadFeatures(hub: Hub, dir: string = DIR): Promise<Feature[]> {
	let files: string[];
	try {
		files = readdirSync(dir).filter((f) => f.endsWith(".js") || (f.endsWith(".ts") && !f.endsWith(".d.ts")));
	} catch {
		return [];
	}
	const out: Feature[] = [];
	for (const f of files.sort()) {
		const mod = await import(pathToFileURL(join(dir, f)).href);
		if (typeof mod.feature === "function") out.push((mod.feature as FeatureFactory)(hub));
	}
	return out;
}
