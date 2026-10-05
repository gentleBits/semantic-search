# Semantic search

Search a collection of resumes by asking in plain words. Each question becomes filters you can see and remove, and an
AI model can rank a short list ("the talented ones at a decent price") and say why.

The demo collection is 2,636 public resumes from Kaggle.

## Install

macOS or Linux (on Windows: use WSL). About 1.2 GB of disk.

```sh
brew install uv node                  # macOS; on Linux see below
git clone https://github.com/gentleBits/semantic-search.git
cd semantic-search
scripts/setup.sh                      # 2–3 minutes; no key, nothing to pay
```

On Linux, install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
[Node.js](https://nodejs.org) 22 or newer first (your distribution's own Node is usually too old).

`scripts/setup.sh` installs everything inside this folder, downloads the resumes and the AI's saved work on them
(about 170 MB in all), and builds the search index.

## Run

```sh
uv run resumes web --open             # opens http://localhost:8765; Ctrl-C stops it
```

You can browse, filter, sort and open the 2,636 resumes right away. No key is needed for that.
([What it looks like](docs/screenshots/app.png).)

## Turn on the chat

The chat and the ranking need an AI model, so you need a key:

```sh
cp .env.example .env                  # then put your OPENAI_API_KEY in .env
set -a; . ./.env; set +a              # load it into this terminal
uv run resumes web --open
```

An [OpenRouter](https://openrouter.ai/keys) key (`OPENROUTER_API_KEY`) works instead: open Settings (the gear) and pick
one of OpenRouter's free models.

The first screen suggests questions to start with. With the default model (`gpt-5-mini`) a question costs well under
a cent; ranking a short list, about a cent.

## More

- **The command line, coding agents (Claude Code, pi, MCP), your own resumes:** [docs/USAGE.md](docs/USAGE.md), [docs/AGENTS.md](docs/AGENTS.md)
- **Settings, accounts and sign-up:** [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
- **Tests and contributing:** [CONTRIBUTING.md](CONTRIBUTING.md)

## If something goes wrong

- **"port in use"**: another `resumes web` is still running; stop it (Ctrl-C in its terminal) and start again.
- **"Node 22 or newer is missing"** or **"uv is missing"**: install it as shown above, then run `scripts/setup.sh` again.
- **The chat says "no key"**: load the key into the same terminal before `uv run resumes web`.

## Licence

The code is MIT ([LICENSE](LICENSE)). The resumes are two CC0 datasets on Kaggle
([one](https://www.kaggle.com/datasets/snehaanbhawal/resume-dataset),
[two](https://www.kaggle.com/datasets/jillanisofttech/updated-resume-dataset)), downloaded by the setup, not stored
here. Their hourly rates are estimates. Third-party code and fonts: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
