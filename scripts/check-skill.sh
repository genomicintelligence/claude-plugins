#!/usr/bin/env bash
# Fail if the skill folder differs from the published skill zip in any byte.
# The folder is generated: fix drift by running scripts/sync-skill.sh (or the
# "Sync skill" workflow), never by hand-editing it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL="$ROOT/plugins/genomic-intelligence/skills/genomic-intelligence"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/x"
python3 -I "$ROOT/scripts/fetch_skill.py" "$TMP/x" >/dev/null

if diff -r "$TMP/x/genomic-intelligence" "$SKILL"; then
  echo "OK: skill folder matches the published zip"
else
  echo "FAIL: skill folder differs from the published zip. It is generated; run scripts/sync-skill.sh or dispatch the Sync skill workflow." >&2
  exit 1
fi
