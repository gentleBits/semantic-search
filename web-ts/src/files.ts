/**
 * Small files of `.resumes/`, written atomically (a temporary file, then a rename; mode 0600), and the O_EXCL lock
 * `users.json` is written under, which the Python `resumes users …` takes too.
 */
import { chmodSync, closeSync, mkdirSync, openSync, renameSync, statSync, unlinkSync, writeFileSync, writeSync } from "node:fs";
import { dirname } from "node:path";
import { ResumesError } from "./errors.js";

export const LOCK_STALE_MS = 10_000; // an older lock was left by a dead process
export const LOCK_WAIT_MS = 5_000;

// waiting is synchronous on purpose: the caller's checks and its write run with no other request in between
const pause = new Int32Array(new SharedArrayBuffer(4));
const sleepSync = (ms: number) => Atomics.wait(pause, 0, 0, ms);

export function writeJsonAtomic(path: string, data: unknown, mode = 0o600, pretty = true): void {
	writeTextAtomic(path, `${JSON.stringify(data, null, pretty ? 1 : 0)}\n`, mode);
}

export function writeTextAtomic(path: string, text: string, mode = 0o600): void {
	mkdirSync(dirname(path), { recursive: true });
	const tmp = `${path}.${process.pid}.${Date.now()}.tmp`;
	writeFileSync(tmp, text, { encoding: "utf-8", mode });
	try {
		chmodSync(tmp, mode);
	} catch {
		// a file system without modes
	}
	renameSync(tmp, path);
}

export function withLock<T>(lockPath: string, fn: () => T, waitMs = LOCK_WAIT_MS): T {
	mkdirSync(dirname(lockPath), { recursive: true });
	const deadline = Date.now() + waitMs;
	for (;;) {
		try {
			const fd = openSync(lockPath, "wx", 0o600);
			writeSync(fd, `${process.pid}\n`);
			closeSync(fd);
			break;
		} catch (e) {
			if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
			try {
				if (Date.now() - statSync(lockPath).mtimeMs > LOCK_STALE_MS) {
					unlinkSync(lockPath);
					continue;
				}
			} catch {
				continue; // gone between the two calls: try again
			}
			if (Date.now() > deadline) throw new ResumesError("BUSY", "the accounts file is being written by another program — try again in a moment");
			sleepSync(20);
		}
	}
	try {
		return fn();
	} finally {
		try {
			unlinkSync(lockPath);
		} catch {
			// already gone
		}
	}
}
