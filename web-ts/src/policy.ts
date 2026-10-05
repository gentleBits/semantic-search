/** The numbers of sign-up and of use: the defaults here, with resumes.toml's [signup] and [limits] over them (`--policy`). */
import { LOGIN_LIMITS, type LoginLimits } from "./auth.js";

/** EU/EEA, the UK with the Crown Dependencies that share +44, Switzerland, the US and Canada. By the number's country,
 * not its calling code: +1 also holds Caribbean premium-rate ranges (+1 876 Jamaica, +1 284 BVI …). */
export const SMS_COUNTRIES = [
	"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
	"PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "GB", "GG", "JE", "IM", "CH", "US", "CA",
];

export interface SignupPolicy {
	sms_countries: string[];
	sms_per_day: number; // the whole server, 24 h
	emails_per_day: number;
	code_ttl: number; // seconds a code is good for
	code_tries: number; // wrong tries before a code is burned
	codes_per_step: number; // codes per step per flow (the first and the resends)
	resend_after: number; // seconds between two codes of a step
	flow_ttl: number; // seconds a flow lives after its last step
	per_email_day: number; // code emails to one address, 24 h, across flows
	per_phone_day: number; // SMS to one number, 24 h, across flows
	per_ip_flows_hour: number;
	per_ip_emails_day: number;
	per_ip_sms_day: number;
	min_password: number;
	work_email: boolean; // refuse personal mailboxes at free providers (gmail.com …); throwaway ones are always refused
}

export interface UsePolicy extends LoginLimits {
	turns_per_day: number; // members: assistant turns, 24 h
	usd_per_day: number; // members: model cost, 24 h
	at_once: number; // members: turns running at the same time, across their searches
	sessions_per_day: number; // members: new sessions, 24 h
	max_usd_per_m_out: number; // members: the priciest model they may pick, $ per million output tokens; a turn's cost
	// is counted only after it ran, so one turn on a very dear model could overshoot the day's allowance
}

export interface Policy {
	signup: SignupPolicy;
	limits: UsePolicy;
}

export const SIGNUP_DEFAULTS: SignupPolicy = {
	sms_countries: SMS_COUNTRIES,
	sms_per_day: 50,
	emails_per_day: 200,
	code_ttl: 600,
	code_tries: 5,
	codes_per_step: 3,
	resend_after: 60,
	flow_ttl: 1800,
	per_email_day: 5,
	per_phone_day: 3,
	per_ip_flows_hour: 10,
	per_ip_emails_day: 10,
	per_ip_sms_day: 5,
	min_password: 10,
	work_email: true,
};

export const LIMIT_DEFAULTS: UsePolicy = { turns_per_day: 100, usd_per_day: 1.0, at_once: 1, sessions_per_day: 10, max_usd_per_m_out: 40, ...LOGIN_LIMITS };

/** The defaults with `raw` (the `--policy` JSON) over them; anything not a number ≥ 0 (or a list of codes, or a yes/no
 * where the default is one) is ignored. */
export function policyOf(raw: unknown): Policy {
	const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, any>;
	const merge = <T extends object>(base: T, over: unknown): T => {
		const out: Record<string, unknown> = { ...(base as Record<string, unknown>) };
		if (over && typeof over === "object") {
			for (const [k, v] of Object.entries(over as Record<string, unknown>)) {
				if (!(k in base)) continue;
				const want = (base as Record<string, unknown>)[k];
				if (Array.isArray(want)) {
					if (Array.isArray(v) && v.every((x) => typeof x === "string")) out[k] = v.map((x) => x.trim().toUpperCase());
				} else if (typeof want === "boolean") {
					if (typeof v === "boolean") out[k] = v;
				} else if (typeof v === "number" && Number.isFinite(v) && v >= 0) out[k] = v;
			}
		}
		return out as T;
	};
	return { signup: merge(SIGNUP_DEFAULTS, r.signup), limits: merge(LIMIT_DEFAULTS, r.limits) };
}
