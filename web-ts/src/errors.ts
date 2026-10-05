/** The engine's error shape: a code the routes map to a status, one line for the person. */
export class ResumesError extends Error {
	constructor(
		public code: string,
		message = "",
		public data: Record<string, unknown> = {},
	) {
		super(message);
		this.name = "ResumesError";
	}
}

/** The engine answered with an error (its JSON body, its status), or did not answer at all (status 0). */
export class EngineError extends Error {
	constructor(
		public status: number,
		public body: { error?: { role?: string; code?: string; text?: string; fix?: unknown }; state?: unknown; [k: string]: unknown },
	) {
		super(body?.error?.text || `the engine answered ${status}`);
		this.name = "EngineError";
	}
	get code(): string {
		return this.body?.error?.code || `HTTP_${this.status}`;
	}
	get text(): string {
		return this.body?.error?.text || this.message;
	}
}
