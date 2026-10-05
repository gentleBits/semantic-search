#!/usr/bin/env bash
# Refresh web-ts/data/disposable-domains.txt from the disposable-email-domains project (CC0). The app server reads the
# file at start-up: commit it, then `scripts/deploy.sh`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/web-ts/data/disposable-domains.txt"
URL="https://raw.githubusercontent.com/disposable-email-domains/disposable-email-domains/main/disposable_email_blocklist.conf"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
curl -sSfL "$URL" -o "$TMP"
n=$(grep -c . "$TMP" || true)
[ "$n" -ge 1000 ] || { echo "update-disposable-domains · only $n lines came back — kept the old file" >&2; exit 1; }
{
	echo "# Throwaway email domains: the sign-up refuses them, and an address whose mail server is one of theirs"
	echo "# (web-ts/src/mail-domains.ts). From github.com/disposable-email-domains/disposable-email-domains (CC0),"
	echo "# disposable_email_blocklist.conf as of $(date +%F); refreshed by scripts/update-disposable-domains.sh."
	tr 'A-Z' 'a-z' < "$TMP" | sed 's/[[:space:]]//g' | grep -v '^$' | sort -u
} > "$OUT"
echo "update-disposable-domains · $(grep -vc '^#' "$OUT") domains → ${OUT#$ROOT/}"
