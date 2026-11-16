---
name: genomic-intelligence
description: Score DNA sequences with the Genomic Intelligence API (transformer DNA language models). Use when the user wants to find or score promoters, enhancers, splice sites, chromatin features or gene expression in a DNA sequence, find genes in a genomic region, or build code that calls the Genomic Intelligence REST API or MCP server. Covers key setup, the bundled Python client, and the input mistakes the API accepts without an error.
---

# Genomic Intelligence

Genomic Intelligence runs six DNA sequence tasks (promoter, splice, enhancer,
chromatin, expression, annotation) and one composite workflow (find genes, then
predict their expression) behind one REST API and one MCP server. Research use
only, not for clinical or diagnostic decisions.

This file routes you. The numbers (length bounds, windows, flanks, model ids,
rate limits) are not here on purpose: they live in the API and in
https://docs.genomicintelligence.ai/AGENTS.md, and the bundled client reads
them at runtime. Do not hard-code any of them.

## Pick a path

1. **MCP tools from the Genomic Intelligence server are available**: use them
   for interactive analysis. Follow the server's own instructions (get a
   sequence handle first, then pass it to a predict tool). Each tool states its
   own limits.
2. **No MCP tools**: call the REST API with the bundled client,
   `scripts/gi_client.py` (one file, needs only `requests`).
3. **The user is building an app or pipeline**: write code against
   `scripts/gi_client.py` in either case. Copy it into their project; do not
   reimplement it.

```python
import os
from gi_client import Client

client = Client(api_key=os.environ["GI_API_KEY"], client_app="gi-skill")
r = client.predict("promoter", sequence=seq, sequence_name="my_seq")
print(r["data"], r["meta"])
```

Pass `client_app="gi-skill"` (or the name of the user's app) so traffic from
this skill can be told apart. It changes nothing about the response.

## Setup

- **REST**: read the key from the `GI_API_KEY` environment variable. Never
  print it, log it, or write it into a file the user will commit.
- **Hosted MCP**: `https://mcp.genomicintelligence.ai/mcp`. It answers without
  a key on a shared demo quota. To use the user's own key, send it as a
  request header: `Authorization: Bearer <key>` or `x-api-key: <key>`.
- **No key yet**: the user requests one from contact@genomicintelligence.ai
  (see https://docs.genomicintelligence.ai/rest-api.md, "Get a key"). The same
  key works for REST and MCP. Do not invent or borrow a key.
- `scripts/requirements.txt` lists the client's only dependency.
  `scripts/quickstart.py` calls every task once and is a good smoke test. Run
  it as `GI_CLIENT_APP=gi-skill python scripts/quickstart.py` so its traffic is
  tagged like the rest of this skill's.

## Silent failures: the API returns 200 for these

Each of these is accepted without an error and returns a confident, wrong
answer. Check them before every call, on either path: MCP tools pass your
input to the same API.

1. **Strand.** Promoter, splice, enhancer and expression read DNA 5' to 3' on
   the gene's sense strand. The wrong strand still scores, at different
   positions, often with high confidence. Reverse-complement minus-strand
   genes yourself. Chromatin is not strand-sensitive. Annotation and the
   composite workflow take the plus-strand region instead. Details: AGENTS.md and
   https://docs.genomicintelligence.ai/tasks.md (the strand column).
2. **Expression input is a TSS-centred window.** Never cut the window by
   hand. Over MCP, fetch it with the `fetch_gene_for_expression` tool and pass
   the handle it returns to the expression predict tool. Over REST, build it
   with `Client.fetch_expression_window(symbol)`. Both read the model list for
   the flank, find the canonical transcript's TSS and handle the minus strand.
   The client returns `{sequence, tss_index, meta}`: send `sequence` and
   `tss_index` unchanged, with `model=meta["default_model"]` unless the user
   named a model. A wrong but in-range `tss_index` scores the wrong region with
   no error. Details: AGENTS.md, "The six tasks".
3. **Read `scored_window`.** Every expression response carries
   `meta.task_specific_counts.scored_window`, the part of the submitted
   sequence the model read. Confirm it sits around the TSS before reporting a
   number.
4. **Never guess `description`.** Expression requires
   `options={"description": ...}` (cell type, assay, condition). It is model
   input, not a label: rewording it changes the prediction. Ask the user for
   it, and hold it fixed across runs you compare.
5. **Splice spans are tokens.** A splice site's `start`/`end` bound the scored
   token, a few bases wide, not the junction base. Report the boundary to
   within the span; do not collapse it to one base.
6. **Enhancer models are fly models.** The enhancer task is trained on
   Drosophila enhancers. A human sequence still scores, and the scores mean
   little. Use it on fly sequence; check `bio_spec.training_data` in the model
   list if in doubt.

Two more that fail loudly but often: `model` is a top-level field
(`options.model` is a `422`), and each task's `options` object is closed, so an
unknown key is a `422`. Branch on `error.code`, not the HTTP status alone.

```python
w = client.fetch_expression_window("HBB")
r = client.predict("expression", w["sequence"], "HBB",
                   model=w["meta"]["default_model"],
                   tss_index=w["tss_index"],
                   options={"description": description_from_user})
print(r["meta"]["task_specific_counts"]["scored_window"])
```

## Long jobs

Long sequences and slow tasks (annotation, the composite workflow) run
asynchronously. Submit, then poll:

```python
job_id = client.submit_async("annotation", sequence=seq, sequence_name="region")
result = client.wait_for_job(job_id)
```

For the composite workflow, call
`client.find_genes_and_predict_expression(seq, description, async_=True)` and
poll `wait_for_job(body["data"]["job_id"])`. A `413 sync_too_large` means
resend the same body asynchronously. A `429` is a rate limit; the client does
not retry, so back off (see `scripts/recipes/04_ratelimit_aware_retry.py`).

## What is in scripts/

- `gi_client.py`: the client. Its docstrings carry the per-method rules.
- `quickstart.py` and `sequences/`: one call per task on bundled fixtures
  (set `GI_CLIENT_APP=gi-skill`).
- `recipes/`: standalone examples (health probe, batch promoters, async
  polling, rate-limit retry, typed error handling).

## Read next

- https://docs.genomicintelligence.ai/AGENTS.md: the contract, every bound,
  the expression rules, model selection, errors. Read it before writing code.
- https://docs.genomicintelligence.ai/rest-api.md: auth, sync, async,
  output formats.
- https://docs.genomicintelligence.ai/mcp.md: the hosted and local MCP server.
- https://docs.genomicintelligence.ai/tasks.md: one card per task, with strand
  and usage tips.
- `GET https://api.genomicintelligence.ai/v1/tasks/{task}/models` (public): the
  live model list for a task, with each model's limits.
