# Genomic Intelligence for Claude Code

A Claude Code plugin for DNA sequence analysis with Genomic Intelligence models:
promoters, splice sites, enhancers, chromatin features, gene annotation and gene
expression.

Research use only. Not for clinical or diagnostic decisions.

## What you get

- **The Genomic Intelligence skill.** It tells Claude how to use the service well,
  and bundles a small Python client for the REST API, worked recipes and example
  sequences.
- **The hosted MCP server** (`https://mcp.genomicintelligence.ai/mcp`). Claude Code
  lists it as `plugin:genomic-intelligence:gi`. Its tools fetch sequences by gene
  name or genomic region and run predictions on them. It also provides two
  prompts, which show up in the slash-command menu as
  `/plugin:genomic-intelligence:gi:gi-promoter-screen (MCP)` and
  `/plugin:genomic-intelligence:gi:gi-expression-screen (MCP)`.

## Install

In Claude Code:

```
/plugin marketplace add genomicintelligence/claude-plugins
/plugin install genomic-intelligence@genomic-intelligence
```

Or from your shell:

```
claude plugin marketplace add genomicintelligence/claude-plugins
claude plugin install genomic-intelligence@genomic-intelligence
```

Restart Claude Code (or run `/reload-plugins`) and check that
`plugin:genomic-intelligence:gi` shows as connected in `/mcp` or
`claude mcp list`.

## API key

A key is optional for the MCP tools. Without one they run on a shared demo quota,
which is fine for trying things out but is shared with everyone else using it.

The Python client in the skill always needs a key.

To use your own key, set `GI_API_KEY` in the environment you start Claude Code
from:

```
export GI_API_KEY=<your key>
claude
```

The same variable is used by the MCP server and by the Python client, so you only
set it once. To get a key, email contact@genomicintelligence.ai. See
[Get a key](https://docs.genomicintelligence.ai/rest-api) in the docs.

If `GI_API_KEY` is set to something that is not a Genomic Intelligence key, tool
calls fail with an `unauthorized` error instead of falling back to the demo
quota. Unset it or fix it.

## If you already added Genomic Intelligence another way

Remove the earlier setup before you rely on the plugin's server:

- **A claude.ai connector, or a `claude mcp add` of the same URL.** Claude Code
  hides the plugin's server in favour of the existing one, so `GI_API_KEY` is not
  sent.
- **A local `uvx gi-mcp` server.** Both servers load, so every tool shows up
  twice.

Run `claude mcp list` to find the earlier entry, then remove it with
`claude mcp remove <name>`. For a claude.ai connector, disconnect it for Claude
Code in `/mcp`.

## Updating

Claude Code does not auto-update plugins from third-party marketplaces unless you
turn it on. Either enable auto-update for this marketplace in `/plugin`, under
Marketplaces, or update by hand:

```
claude plugin marketplace update genomic-intelligence
claude plugin update genomic-intelligence@genomic-intelligence
```

Then restart Claude Code.

## Docs

- Documentation: https://docs.genomicintelligence.ai
- MCP server: https://docs.genomicintelligence.ai/mcp
- REST API: https://docs.genomicintelligence.ai/rest-api
- Using Genomic Intelligence from coding agents: https://docs.genomicintelligence.ai/agents

## About this repository

The skill folder, `plugins/genomic-intelligence/skills/genomic-intelligence/`, is
generated from the published skill at
https://docs.genomicintelligence.ai/skill/genomic-intelligence.zip and synced
daily. Please open an issue rather than a pull request against it: hand edits
are overwritten by the next sync, and CI fails on them.

Released under the MIT No Attribution licence (MIT-0). See [LICENSE](LICENSE).
