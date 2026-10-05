/**
 * A typed phone number in international form → E.164 and its country (the allowlist is by country, so a number whose
 * country cannot be told is refused). `isPossible()`, not `isValid()`: stale range metadata must not refuse a real number.
 */
import { parsePhoneNumberFromString } from "libphonenumber-js/max"; // full metadata: a +1 number's country needs it

export type Phone = { ok: true; e164: string; country: string } | { ok: false; why: "BAD_PHONE" | "PHONE_COUNTRY"; country?: string };

export function parsePhone(input: unknown, allowed: readonly string[]): Phone {
	let s = String(input ?? "").trim();
	if (!s || s.length > 40) return { ok: false, why: "BAD_PHONE" };
	s = s.replace(/^00/, "+");
	if (!s.startsWith("+")) return { ok: false, why: "BAD_PHONE" };
	if (/[^\d\s+().\-/]/.test(s)) return { ok: false, why: "BAD_PHONE" };
	const p = parsePhoneNumberFromString(s);
	if (!p || !p.isPossible() || !/^\+[1-9]\d{6,14}$/.test(p.number) || !p.country) return { ok: false, why: "BAD_PHONE" };
	if (!allowed.includes(p.country)) return { ok: false, why: "PHONE_COUNTRY", country: p.country };
	return { ok: true, e164: p.number, country: p.country };
}

/** "+40712345678" → "+40 712 345 678". */
export function formatPhone(e164: string): string {
	return parsePhoneNumberFromString(e164)?.formatInternational() || e164;
}
