---
description: Remove a resume filter — /drop f2, /drop elixir, /drop rank (mechanical — nothing is searched again)
argument-hint: f2 | word | rank
allowed-tools: Bash(resumes drop:*)
---
!`resumes drop $ARGUMENTS`

Relay the first line (what was removed and the new count), then the links block if there is one (the numbered lines), then the final `—` state line, exactly as printed. If the output is an error (`UNKNOWN_FILTER`, `AMBIGUOUS_FILTER`, `NO_RANKING`, …), relay that line: it lists the active filters. Do not add commentary, do not paste cards, do not call tools.
