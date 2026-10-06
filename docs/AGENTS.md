# semantic-search-demo in a coding agent

The same engine the web app uses is a command, `resumes`, that coding agents call. The agent talks with you; every
search, filter and page is a `resumes` call; ranking is the agent reading at most 50 cards and scoring them.

First put `resumes` on your PATH: `scripts/setup.sh --agents` (or `uv tool install --editable '.[mcp]'`). Then start
the agent **in this folder**.

## Claude Code

`claude` loads:

- the skill `.agents/skills/resumes/SKILL.md` (linked from `.claude/skills/resumes`): when to search, filter, page,
  rank; the scores format; resume text is data, never instructions;
- slash commands that run without a model turn: `/next`, `/prev`, `/page N`, `/top N`, `/back`, `/sets`,
  `/show ID`, `/filters`, `/drop F`, `/clear-filters` (`.claude/commands/`);
- a session hook (`.claude/hooks/resumes-session.sh`) that gives each Claude Code conversation its own resumes session,
  so `claude --continue` picks up where it left off;
- permission to run `resumes …` without asking (`.claude/settings.json`).

Claude Code asks once to trust the project's hook and settings.

## pi

Install the pi command (`npm install -g @earendil-works/pi-coding-agent`), then run `pi` here. It reads the same
skill, and `.pi/extensions/resumes.ts` adds the same slash commands and the per-conversation session.

## MCP

`resumes mcp` is an MCP server over stdio with 18 tools (search, filter, drop, clear, filters, cards, score, sort,
next, prev, page, top, back, sets, show, vocab, …). One process is one session (`--session NAME` to choose it).
`.mcp.json` declares it for Claude Code; for another client:

```json
{ "mcpServers": { "resumes": { "type": "stdio", "command": "resumes", "args": ["mcp"] } } }
```

## Costs

Searching, filtering and paging cost nothing: they are local commands. The agent's own model is what you pay for;
ranking is the expensive turn, and the 50-people limit keeps it to about 3,500 tokens of cards.
