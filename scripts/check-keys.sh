#!/usr/bin/env bash
# Fail if anything that looks like a Genomic Intelligence API key is in the
# working tree, or with --history, anywhere in git history. Exact placeholder
# strings listed in scripts/key-allowlist.txt are ignored.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PATTERN='gi_[A-Za-z0-9_-]{20,}'
ALLOW="$(grep -v -e '^#' -e '^$' scripts/key-allowlist.txt)"

# Reads "location:match" lines on stdin, prints those whose match is not allowlisted.
not_allowed() {
  local line m
  while IFS= read -r line; do
    m="${line##*:}"
    grep -qxF -- "$m" <<<"$ALLOW" || echo "$line"
  done
}

status=0
hits="$(grep -rnoIE --exclude-dir=.git "$PATTERN" . | not_allowed || true)"
if [[ -n "$hits" ]]; then
  echo "$hits"
  echo "FAIL: key-shaped string in the working tree" >&2
  status=1
fi

if [[ "${1:-}" == "--history" ]]; then
  hits="$(git log -p --all --format='commit %H' | grep -noE "$PATTERN" | not_allowed || true)"
  if [[ -n "$hits" ]]; then
    echo "$hits"
    echo "FAIL: key-shaped string in git history" >&2
    status=1
  fi
fi

if [[ $status == 0 ]]; then
  echo "OK: no key-shaped strings outside the allowlist${1:+ (tree and history)}"
fi
exit $status
