---
description: One resume by id — its card and link (add --full for the whole markdown)
argument-hint: r000412 [--full]
allowed-tools: Bash(resumes show:*)
---
!`resumes show $ARGUMENTS`

Relay the output above to the user exactly as printed (the card and its `→` link, or the full markdown). If it is a one-line error (`UNKNOWN_DOC_ID …`), relay that line. Do not add commentary or call tools.
