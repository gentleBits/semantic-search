#!/usr/bin/env bash
# Downloads the two public resume datasets (Kaggle, CC0) into data/. No Kaggle account is needed.
#
#     scripts/get-data.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${RESUMES_DATA_DIR:-$ROOT/data}"
KAGGLE="https://www.kaggle.com/api/v1/datasets/download"

sha256() {
	if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() { # dataset, file inside its zip, target, expected sha256
	local ref=$1 inner=$2 target=$3 want=$4 tmp got
	if [ -f "$target" ] && [ "$(sha256 "$target")" = "$want" ]; then
		echo "get-data · $(basename "$target") is already there"
		return
	fi
	tmp="$(mktemp -d)"
	trap 'rm -rf "$tmp"' RETURN
	echo "get-data · downloading $ref from Kaggle …"
	curl -fsSL --retry 3 -o "$tmp/data.zip" "$KAGGLE/$ref"
	unzip -p "$tmp/data.zip" "$inner" >"$tmp/file"
	got="$(sha256 "$tmp/file")"
	if [ "$got" != "$want" ]; then
		echo "get-data · $ref: the file has changed on Kaggle (sha256 $got, expected $want); nothing was written" >&2
		exit 1
	fi
	mkdir -p "$DATA"
	mv "$tmp/file" "$target"
	echo "get-data · $(basename "$target") ($(wc -c <"$target" | tr -d ' ') bytes)"
}

fetch snehaanbhawal/resume-dataset Resume/Resume.csv "$DATA/resume_dataset_livecareer.csv" \
	816a7cd985a9a41e3ce8d1e83cb7e1beb1e854a89c1cf1fd0d75494faaa0559d
fetch jillanisofttech/updated-resume-dataset UpdatedResumeDataSet.csv "$DATA/UpdatedResumeDataSet.csv" \
	c076ae68623eab41403dd6a863aef089b38e4c7e1294d17f9593b67ad2f181fa
