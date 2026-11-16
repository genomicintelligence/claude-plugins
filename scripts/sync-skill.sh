#!/usr/bin/env bash
# Replace plugins/genomic-intelligence/skills/genomic-intelligence with the
# contents of the published skill zip.
#
#   scripts/sync-skill.sh          sync, report whether content changed
#   scripts/sync-skill.sh --bump   also bump plugin.json's patch version on change
#
# Prints "changed" or "unchanged" as its last line. When GITHUB_OUTPUT is set it
# also writes changed=true|false and, after a bump, version=<new>.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL="$ROOT/plugins/genomic-intelligence/skills/genomic-intelligence"
MANIFEST="$ROOT/plugins/genomic-intelligence/.claude-plugin/plugin.json"
BUMP=0
[[ "${1:-}" == "--bump" ]] && BUMP=1

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/x"
python3 -I "$ROOT/scripts/fetch_skill.py" "$TMP/x" >/dev/null
NEW="$TMP/x/genomic-intelligence"

if [[ -d "$SKILL" ]] && diff -r "$NEW" "$SKILL" >/dev/null; then
  echo "skill folder already matches the published zip"
  [[ -n "${GITHUB_OUTPUT:-}" ]] && echo "changed=false" >> "$GITHUB_OUTPUT"
  echo "unchanged"
  exit 0
fi

if [[ -d "$SKILL" ]]; then
  echo "content differs from the published zip:"
  diff -rq "$NEW" "$SKILL" | sed "s|$TMP/x/||; s|$ROOT/||" || true
fi
rm -rf "$SKILL"
mkdir -p "$(dirname "$SKILL")"
cp -R "$NEW" "$SKILL"

if [[ "$BUMP" == 1 ]]; then
  NEWVER="$(python3 -I - "$MANIFEST" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
m = re.search(r'"version":\s*"(\d+)\.(\d+)\.(\d+)"', text)
if not m:
    sys.exit("plugin.json has no x.y.z version")
new = f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"
text = text[:m.start()] + f'"version": "{new}"' + text[m.end():]
open(path, "w", encoding="utf-8").write(text)
print(new)
PY
)"
  echo "bumped plugin version to $NEWVER"
  [[ -n "${GITHUB_OUTPUT:-}" ]] && echo "version=$NEWVER" >> "$GITHUB_OUTPUT"
fi
[[ -n "${GITHUB_OUTPUT:-}" ]] && echo "changed=true" >> "$GITHUB_OUTPUT"
echo "changed"
