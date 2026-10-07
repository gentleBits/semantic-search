// The check page's number field (public/js/phone.js), read the way a person reads it: the box's +code, then the field.
import assert from "node:assert/strict";
import { test } from "node:test";
import { SMS_COUNTRIES } from "../src/policy.js";
import { parsePhone } from "../src/phone.js";

// @ts-ignore: the page's own JavaScript, no types
const P = await import("../public/js/phone.js");
// @ts-ignore
const examples = (await import("../public/vendor/phone-examples.js")).default;
await P.load();
const L = (globalThis as any).libphonenumber;
const reads = (country: string, field: string) => `+${L.getCountryCallingCode(country)}${field.replace(/\D/g, "")}`;

test("every country's grey example, after its +code, is a real number: no trunk 0 (\"+40 | 0712 …\" is a wrong number)", () => {
	for (const c of SMS_COUNTRIES) {
		const real = L.getExampleNumber(c, examples).number;
		assert.equal(reads(c, P.example(c)), real, `${c}: "${P.example(c)}"`);
	}
});

test("a number typed key by key, with the habitual 0, without it, or with its +code, reads and goes as the real number; a delete never sticks", () => {
	for (const c of SMS_COUNTRIES) {
		const n = L.getExampleNumber(c, examples);
		const ways: Record<string, string> = { "with its 0": n.formatNational().replace(/\D/g, ""), without: n.nationalNumber, "with +code": `+${L.getCountryCallingCode(c)}${n.nationalNumber}` };
		for (const [how, keys] of Object.entries(ways)) {
			let v = "";
			let country = how === "with +code" ? (c === "US" ? "DE" : "US") : c;
			for (const k of keys) ({ text: v, country } = P.typed(v + k, country, SMS_COUNTRIES, true));
			assert.equal(reads(country, v), n.number, `${c} ${how}: the field says "${v}" next to ${country}`);
			assert.equal(P.full(v, country), n.number, `${c} ${how}: what is sent`);
			assert.ok(parsePhone(P.full(v, country), SMS_COUNTRIES).ok, `${c} ${how}: the server takes it`);
			for (let i = 0; i < 40 && v; i++) {
				const next = P.typed(v.slice(0, -1), country, SMS_COUNTRIES, false).text;
				assert.ok(next.length < v.length, `${c} ${how}: a delete sticks at "${v}"`);
				v = next;
			}
		}
	}
});

test("the server's number comes back into the field as written after its code", () => {
	assert.deepEqual(P.split("+40 712 345 678", SMS_COUNTRIES), { country: "RO", text: "712 345 678" });
	assert.deepEqual(P.split("+44 7400 123456", SMS_COUNTRIES), { country: "GB", text: "7400 123456" });
	assert.deepEqual(P.split("+7 912 345-67-89", SMS_COUNTRIES), { country: null, text: "+7 912 345-67-89" }, "a country not in the list stays whole");
});
