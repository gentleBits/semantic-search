#!/usr/bin/env bash
# From a fresh clone to a working install, with no key and at no cost: checks the tools, installs the Python and
# Node parts, downloads the public resume data and the model's saved outputs, builds the corpus and the index.
#
#     scripts/setup.sh            # then: uv run resumes web --open
#     scripts/setup.sh --agents   # also puts `resumes` on the PATH, for Claude Code, pi and MCP clients
set -euo pipefail
cd "$(dirname "$0")/.."

agents=0
for a in "$@"; do
	case $a in
	--agents) agents=1 ;;
	-h | --help) sed -n '2,6p' "$0" | cut -c3-; exit 0 ;;
	*) echo "setup · unknown option $a" >&2; exit 2 ;;
	esac
done

say() { printf 'setup · %s\n' "$*"; }
stop() { printf 'setup · %s\n' "$*" >&2; exit 1; }
logged() { # a step whose output only matters when it fails
	"$@" >.setup.log 2>&1 || { tail -25 .setup.log >&2; stop "failed: $* (the whole output is in .setup.log)"; }
}

mac=0
[ "$(uname -s)" = Darwin ] && mac=1
command -v uv >/dev/null || stop "uv is missing. Install it: $([ $mac = 1 ] && echo 'brew install uv' || echo 'curl -LsSf https://astral.sh/uv/install.sh | sh')"
if ! command -v node >/dev/null; then # Homebrew's node@22 is installed off the PATH
	for d in /opt/homebrew/opt/node@22/bin /usr/local/opt/node@22/bin; do [ -x "$d/node" ] && export PATH="$d:$PATH" && break; done
fi
command -v node >/dev/null || stop "Node 22.19 or newer is missing. Install it: $([ $mac = 1 ] && echo 'brew install node' || echo 'https://nodejs.org (nvm or NodeSource; the distribution'"'"'s own package is usually too old)')"
node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit(a > 22 || (a === 22 && b >= 19) ? 0 : 1)' || stop "Node $(node --version) is too old: 22.19 or newer is needed"
for tool in curl unzip; do command -v $tool >/dev/null || stop "$tool is missing"; done

say "Python 3.13 and its packages (uv) …"
logged uv sync --extra web --extra mcp --extra dev
say "the app server's packages (npm) …"
logged npm ci --prefix web-ts --no-audit --no-fund
logged npm run --prefix web-ts build
say "the public resume datasets (Kaggle) …"
scripts/get-data.sh
say "the model's saved outputs, so that building needs no key …"
uv run --no-sync resumes index seed load --tests
say "the corpus (about 30 s) …"
logged uv run --no-sync resumes corpus build
say "the index (about a minute) …"
logged uv run --no-sync resumes index build
rm -f .setup.log

if [ $agents = 1 ]; then
	say "resumes on the PATH (uv tool install) …"
	uv tool install --editable '.[mcp]' --force -q
	command -v resumes >/dev/null || say "add $(uv tool dir --bin) to your PATH, then open a new terminal"
fi

cat <<'EOF'
setup · done.

    uv run resumes web --open      the web UI on http://localhost:8765 (browse, filter, sort, open resumes: no key needed)

  The chat and the ranking need a model: export OPENAI_API_KEY (or OPENROUTER_API_KEY for its free models)
  before starting `resumes web`; see .env.example. The README has the rest.
EOF
