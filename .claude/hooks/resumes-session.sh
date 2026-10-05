#!/bin/bash
# SessionStart hook: bind this Claude Code conversation to one resumes session. Exports RESUMES_SESSION=cc-<session_id>
# through CLAUDE_ENV_FILE so every `resumes …` call lands in the same session; resuming the conversation resumes its sets.
input=$(cat)
sid=$(printf '%s' "$input" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
[ -z "$sid" ] && exit 0
if [ -n "$CLAUDE_ENV_FILE" ]; then
  printf 'export RESUMES_SESSION=cc-%s\n' "$sid" >> "$CLAUDE_ENV_FILE"
fi
if [ -n "$RESUMES_HOOK_LOG" ]; then
  printf '%s SessionStart sid=%s env_file=%s\n' "$(date +%T)" "$sid" "${CLAUDE_ENV_FILE:-unset}" >> "$RESUMES_HOOK_LOG"
fi
exit 0
