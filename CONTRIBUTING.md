# Contributing

Thank you for looking. A few things to know first:

- **This repository is published from a private one.** Pull requests are welcome and read; an accepted one is applied
  upstream with you credited as co-author, and reaches this repository with the next publication. Your pull request
  is then closed with a link to that commit, rather than merged here.
- **Tests need no key.** `scripts/setup.sh`, then `uv run pytest` (the app server's tests included) and
  `cd web-ts && npm test`. The first run builds the tests' own index (about two minutes). Please add a test with a
  change in behaviour.
- **Keep it small and plain:** the page has no build step, comments say why rather than what, and an error is one
  line that says how to fix it.
- **Never put a key in an issue, a commit or a test.** Tests use fake keys and pi-ai's faux model.

Questions and ideas: open an issue.
