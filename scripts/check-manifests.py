#!/usr/bin/env python3
"""Structure checks that do not need the Claude Code CLI.

Run: python3 -I scripts/check-manifests.py

CI also runs `claude plugin validate --strict`; this script covers the
repo-specific rules that validate does not know about.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "genomic-intelligence"
problems = []


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{path.relative_to(ROOT)}: {exc}")
        return {}


market = load(ROOT / ".claude-plugin" / "marketplace.json")
manifest = load(PLUGIN / ".claude-plugin" / "plugin.json")
mcp = load(PLUGIN / ".mcp.json")

# marketplace.json
if market.get("name") != "genomic-intelligence":
    problems.append("marketplace name must be genomic-intelligence")
if not isinstance(market.get("owner"), dict) or not market["owner"].get("name"):
    problems.append("marketplace owner.name is required")
entries = market.get("plugins") or []
if [e.get("name") for e in entries] != ["genomic-intelligence"]:
    problems.append("marketplace must list exactly the genomic-intelligence plugin")
for e in entries:
    if e.get("source") != "./plugins/genomic-intelligence":
        problems.append("plugin entry source must be ./plugins/genomic-intelligence")
    if "version" in e:
        problems.append("plugin entry must not set version (plugin.json owns it)")

# plugin.json
if manifest.get("name") != "genomic-intelligence":
    problems.append("plugin.json name must be genomic-intelligence")
if not re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("version", ""))):
    problems.append("plugin.json needs an explicit x.y.z version")
for field in ("description", "author", "homepage", "repository", "license"):
    if not manifest.get(field):
        problems.append(f"plugin.json is missing {field}")

# .mcp.json
servers = mcp.get("mcpServers", {})
gi = servers.get("gi", {})
if list(servers) != ["gi"]:
    problems.append(".mcp.json must define exactly one server, gi")
if gi.get("type") != "http" or gi.get("url") != "https://mcp.genomicintelligence.ai/mcp":
    problems.append(".mcp.json gi must be type http at https://mcp.genomicintelligence.ai/mcp")
headers = gi.get("headers", {})
if headers.get("x-api-key") != "${GI_API_KEY:-}":
    problems.append('.mcp.json must send "x-api-key": "${GI_API_KEY:-}" (an unset key must send an empty header)')
if any(k.lower() == "authorization" for k in headers):
    problems.append(".mcp.json must not send Authorization: an empty Bearer value is a 401, not the demo key")
if headers.get("X-GI-Client-App") != "gi-claude-plugin":
    problems.append(".mcp.json must send X-GI-Client-App: gi-claude-plugin")

# components: skill only, no hooks or commands
if not (PLUGIN / "skills" / "genomic-intelligence" / "SKILL.md").is_file():
    problems.append("skills/genomic-intelligence/SKILL.md is missing")
for banned in ("hooks", "commands", "agents"):
    if (PLUGIN / banned).exists():
        problems.append(f"plugin must not ship {banned}/")

# licence
# The root LICENSE is the one to edit. plugins/genomic-intelligence/LICENSE is a
# byte copy so the licence text travels into users' plugin cache. To swap the
# licence: replace LICENSE, `cp LICENSE plugins/genomic-intelligence/LICENSE`,
# change plugin.json "license", the README's last line, and the first-line
# check below.
lic = ROOT / "LICENSE"
if not lic.is_file() or not lic.read_text(encoding="utf-8").startswith("MIT No Attribution"):
    problems.append("LICENSE must be the MIT-0 text (or update this check with the licence swap)")
plic = PLUGIN / "LICENSE"
if not plic.is_file() or plic.read_bytes() != lic.read_bytes():
    problems.append("plugins/genomic-intelligence/LICENSE must be a byte copy of LICENSE (cp LICENSE plugins/genomic-intelligence/LICENSE)")

# forbidden figures, same regex as the skill's own test upstream
FORBIDDEN = re.compile(r"g0-|[0-9]{1,3},[0-9]{3} ?bp|[0-9]{4,} ?bp|recommended_flank_bp: *[0-9]|gi_[A-Za-z0-9_-]{10,}")
for path in (ROOT / "README.md", PLUGIN / "README.md", PLUGIN / ".claude-plugin" / "plugin.json",
             ROOT / ".claude-plugin" / "marketplace.json"):
    if not path.is_file():
        problems.append(f"{path.relative_to(ROOT)} is missing")
        continue
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FORBIDDEN.search(line):
            problems.append(f"{path.relative_to(ROOT)}:{n}: model id, bp figure or key: {line.strip()}")

if (ROOT / "README.md").is_file() and (PLUGIN / "README.md").is_file():
    if (ROOT / "README.md").read_bytes() != (PLUGIN / "README.md").read_bytes():
        problems.append("README.md and plugins/genomic-intelligence/README.md must be identical")

if problems:
    print("FAIL:")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)
print("OK: manifests, MCP config, components, licence and READMEs pass")
