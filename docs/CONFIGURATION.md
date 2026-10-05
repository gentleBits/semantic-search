# Configuration

## resumes.toml

The one settings file, read from the folder `resumes` runs in. Each option is explained where it stands.

| Section | What it sets |
|---|---|
| `[corpus]`, `[[sources]]` | where resumes come from (the two public datasets, `data/inbox/` for your own), the currency rates are stored in |
| `[index]` | the embedding model, the extraction model (`none` to skip it), the semantic threshold, chunk and card sizes |
| `[query]` | the ranking limit (`max_cards = 50`), page size, how long sessions are kept, links to an ATS (`link_template`) |
| `[web]` | host and port, the default model and thinking level, the ranking batches, the collection's name and words, the starter questions |
| `[signup]` | optional sign-up (off by default): daily caps on emails and SMS, business email only, SMS countries |
| `[limits]` | what people who signed up may use: turns, cost and new sessions a day, the dearest model |
| `[fixture]` | the synthetic test resumes (`resumes fixture …`; you will not need it) |

## Environment

| Variable | Used for |
|---|---|
| `OPENAI_API_KEY` | the chat, ranking, `--text`, `--like`, `judge`, and building what the saved outputs do not cover |
| `OPENROUTER_API_KEY` | the chat and ranking on OpenRouter's free models |
| `RESEND_API_KEY`, `RESEND_FROM`, `SAKARI_ACCOUNT_ID`, `SAKARI_CLIENT_ID`, `SAKARI_CLIENT_SECRET` | sign-up's email and SMS codes |
| `RESUMES_SIGNUP_DRYRUN=1` | sign-up with nothing sent: the codes are printed |
| `RESUMES_SESSION` | which command-line session to use |
| `RESUMES_INDEX_DIR` | query another index folder |
| `RESUMES_TIME_FACTOR` | relaxes the tests' speed checks on a slower machine |

`.env.example` lists the keys; copy it to `.env` (ignored by git) and load it before starting `resumes web`.

## Files the app writes

| Path | Holds |
|---|---|
| `corpus/`, `index/` | the built corpus and index; `index/cache/` keeps every model output by content |
| `.resumes/sessions/` | conversations and their result sets |
| `.resumes/settings.json` | the model each choice in Settings made; keys you put there (mode 0600) |
| `.resumes/users.json`, `secret` | accounts (scrypt hashes) and the key that signs login cookies |
| `.test/` | the tests' own index |

None of these are in git.

## Accounts

Without an account the app is open to whoever reaches it, which suits a tool on your own machine.
`uv run resumes users add NAME` makes one (it asks for a password). From then on the app asks everyone to log in, and
each person sees only their own searches. `resumes users list` and `resumes users remove NAME` manage them.

`resumes web --signup` lets people make their own account: a code by email, a code by SMS, then a password. It needs
Resend and Sakari keys (see Environment above); `RESUMES_SIGNUP_DRYRUN=1` tries it with the codes printed in the
terminal instead of sent.

## Behind a proxy

To serve the app on a domain, run it behind a TLS proxy (Caddy, nginx) and tell it the public name:
`resumes web --public-host search.example.com`. The app only answers requests addressed to its own names (a guard
against DNS rebinding), keeps the login cookie `Secure` over https, and trusts `X-Forwarded-For` only from localhost.
Add accounts with `resumes users add NAME` first: without one, the app is open to anyone who reaches it.
