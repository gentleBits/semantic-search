/**
 * resumes — pi extension: binds this pi session to one resumes session (RESUMES_SESSION=pi-<id>) and adds the
 * mechanical slash commands. Their output is posted with `triggerTurn: false`: it enters the context (so "tell me
 * about the third one" works) but no model turn runs; the TUI shows only the links block and the state line.
 * The skill itself is read from .agents/skills/resumes/SKILL.md (pi's own skill location).
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Box, Text } from "@earendil-works/pi-tui";

// `verb`: the CLI verb when it differs from the command name (/clear is taken by the agents themselves).
const COMMANDS: Array<{ name: string; description: string; args: boolean; verb?: string }> = [
	{ name: "next", description: "Next page of the current resume result set (no model call)", args: false },
	{ name: "prev", description: "Previous page of the current resume result set", args: false },
	{ name: "page", description: "Page N of the current resume result set", args: true },
	{ name: "top", description: "First N of the current resume result set", args: true },
	{ name: "back", description: "Back to the parent resume result set", args: false },
	{ name: "sets", description: "The resume result sets of this session as a tree", args: false },
	{ name: "show", description: "One resume by id: its card and link (add --full for the markdown)", args: true },
	{ name: "filters", description: "The active resume filters, what each does to the count, and the ranking", args: false },
	{ name: "drop", description: "Remove a resume filter: /drop f2, /drop elixir, /drop rank (nothing is searched again)", args: true },
	{ name: "clear-filters", verb: "clear", description: "Remove every resume filter and the ranking: all CVs", args: false },
];

/** The part of a page the user should see: the `links:` block and the final `—` state line; everything if there is no links block. */
function visiblePart(text: string): string {
	const lines = text.split("\n");
	const at = lines.indexOf("links:");
	if (at < 0) return text;
	return [lines[0], ...lines.slice(at + 1)].join("\n");
}

export default function (pi: ExtensionAPI) {
	pi.registerMessageRenderer("resumes", (message, { expanded }, theme) => {
		const content = typeof message.content === "string" ? message.content : String(message.content ?? "");
		const shown = expanded ? content : visiblePart(content);
		const box = new Box(1, 1, (t) => theme.bg("customMessageBg", t));
		box.addChild(new Text(shown, 0, 0));
		return box;
	});

	pi.on("session_start", async (_event, ctx) => {
		const id = ctx.sessionManager.getSessionId?.() ?? `ephemeral-${Date.now().toString(36)}`;
		process.env.RESUMES_SESSION = `pi-${id}`;
	});

	for (const { name, description, args, verb } of COMMANDS) {
		pi.registerCommand(name, {
			description,
			handler: async (rawArgs, ctx) => {
				const argv = [verb ?? name, ...(args ? rawArgs.trim().split(/\s+/).filter(Boolean) : [])];
				const r = await pi.exec("resumes", argv, { timeout: 15000 });
				const text = [r.stdout?.trimEnd(), r.stderr?.trimEnd()].filter(Boolean).join("\n") || `resumes ${argv.join(" ")}: no output (exit ${r.code})`;
				pi.sendMessage({ customType: "resumes", content: text, display: true }, { triggerTurn: false });
				if (r.code !== 0 && ctx.hasUI) ctx.ui.notify(text.split("\n")[0], "warning");
			},
		});
	}
}
