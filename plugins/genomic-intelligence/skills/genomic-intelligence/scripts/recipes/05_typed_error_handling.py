"""Recipe 05 — typed error handling.

Switch on `error.code`, never on `error.message`. Every code from
https://docs.genomicintelligence.ai/reference/errors.md is mapped here
to one of four buckets — retryable, permanent, server-bug, or
proxy-timeout (the enveloped-less 504 special case) — plus the action
your code should take.

Use this as the template for your own error handler. Copy `handle()`
into your codebase, register it as your one place to translate API
errors to your domain's exception types.

    GI_API_KEY=gi_… python3 05_typed_error_handling.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Optional

import requests


BASE_URL = os.environ.get("GI_BASE_URL", "https://api.genomicintelligence.ai")
API_KEY = os.environ.get("GI_API_KEY")


@dataclass
class APIError(Exception):
    status: int
    code: str
    message: str
    request_id: Optional[str]
    details: Any
    bucket: str       # "retryable" | "permanent" | "server_bug" | "proxy_timeout"
    action: str       # human-readable action hint for the caller


def per_field(details: Any) -> list:
    """Normalise `error.details` for a `validation_failed` into a list.

    The service usually emits `{"errors": [...]}`, matching the
    `ValidationFailedDetails` schema; older releases emitted the bare FastAPI
    array, and splice's response-size cap emits a different object entirely
    (`{task, record_count, maximum_records, sequence_length, threshold}`, no
    `errors` key). This helper accepts either list shape and treats anything
    else — including that one — as "no structured detail"; read
    `error.message` there.
    """
    if isinstance(details, list):
        return details
    if isinstance(details, dict):
        errors = details.get("errors")
        return errors if isinstance(errors, list) else []
    return []


def handle(response: requests.Response) -> dict:
    """Convert an API response into either a parsed body or a raised APIError.

    Single source of truth for every code in the catalogue. If we ever add
    a new error code, the `unknown_code` branch surfaces it cleanly.
    """
    if response.ok:
        return response.json()

    # 504 from the edge proxy is the one response on the contract that
    # is NOT the unified error envelope. Treat as a special case.
    if response.status_code == 504:
        raise APIError(
            status=504, code="gateway_timeout",
            message="Upstream proxy read timeout (300 s).",
            request_id=None, details=None,
            bucket="proxy_timeout",
            action="Retry the same request with header `Prefer: respond-async`.",
        )

    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    err = (body or {}).get("error") or {}
    code = err.get("code", "unknown_code")
    msg = err.get("message", "")
    request_id = err.get("request_id")
    details = err.get("details")

    # Retryable transient — the API or upstream is asking you to back off.
    if code == "too_many_requests":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="retryable",
                       action="Honour Retry-After. Both rate-bucket and concurrency 429s use this code.")
    if code == "rate_limited":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="retryable",
                       action="Same treatment as too_many_requests — honour Retry-After.")
    if code == "model_loading":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="retryable",
                       action="Wait ~30 s and retry. The model is warming up.")
    if code == "service_unavailable":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="retryable",
                       action="Backoff and retry. Server is starting up or shutting down.")

    # Permanent client errors — surface to the caller, don't retry.
    if code == "bad_request":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="Fix the HTTP-level precondition and re-issue.")
    if code == "unauthorized":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="Check Authorization header. WWW-Authenticate splits invalid_request vs invalid_token.")
    if code == "forbidden":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="Key is registered but disabled. Contact the API operator.")
    if code == "not_found":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="Unknown route, unknown job, or cross-tenant access (returned as 404 not 403).")
    if code == "model_not_found":
        # details.available_models is the typed list of alternatives.
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action=f"Pick a model from details.available_models: {details}.")
    if code == "validation_failed":
        # `details` is usually per-field validation output. Read it
        # DEFENSIVELY: the service wraps the FastAPI errors in an object
        # ({"errors": [...]}), matching the schema; the bare array
        # ([{loc, msg, type}, ...]) is the legacy shape this helper still
        # tolerates. Splice's response-size cap is a validation_failed with
        # neither shape — {task, record_count, maximum_records,
        # sequence_length, threshold} — and neither is expression's refusal of
        # a window with too little real (non-N) sequence around the TSS,
        # whose details are {invalid_characters: [], invalid_positions: [],
        # valid_characters: [...]} with empty lists. So never index
        # details["errors"], and never infer the cause from details.
        # Never make control flow depend on its shape — or on `loc`. Sequence
        # bound violations report at ["body","sequence"], an unknown options
        # key at ["body","options","<key>"], but expression's tss_index checks
        # come from a whole-model validator and report at ["body"] — there is
        # no ["body","tss_index"] to match on. Switch on `code` only.
        raise APIError(response.status_code, code, msg, request_id, per_field(details),
                       bucket="permanent",
                       action="Surface message verbatim — it's deterministic. Never branch on details[].loc.")
    if code == "task_not_supported_by_model":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="Pick a different model — see details.supported_tasks.")
    if code == "payload_too_large":
        # 413 #1: the 16 MiB raw-body cap, enforced before parsing. NOT the
        # sequence-length cap — an over-500,000 bp sequence is a 422.
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="Body exceeded 16 MiB. Split the request or use sequence references if available.")
    if code == "sync_too_large":
        # 413 #2: an operation refusing synchronous JSON delivery above its
        # published cap — the composite find-genes-and-predict-expression
        # workflow above 50,000 bp, or annotation above 200,000 bp. details
        # carries {sequence_length, threshold}. Resend the identical body async.
        # (BED/GFF3 annotation responses are uncapped and never land here.)
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="Resend the same body with `Prefer: respond-async`.")
    if code == "unsupported_format":
        # 415. Per-task format sets differ (promoter json|bed|bedgraph,
        # splice json|bed|gff3, enhancer json|bedgraph, chromatin json|bed,
        # annotation json|bed|gff3, expression json only) and there is no
        # silent fallback to JSON. The message names the supported values.
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="The Accept / ?format= value is not in this task's allowed set.")
    if code == "conflict":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="Reserved (e.g. busy-model unload conflict). Retry once after delay.")
    if code == "job_expired":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="permanent",
                       action="Async result evicted (24 h TTL or process restart). Resubmit the original POST.")

    # Server-side capacity / timing — retry once, then shrink the request.
    if code == "timeout":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="retryable",
                       action="Inference exceeded the server deadline. Resubmit with `Prefer: respond-async`.")
    if code == "insufficient_memory":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="retryable",
                       action="GPU memory exhausted. Retry after a delay, or submit a shorter sequence.")

    # Server bug — capture and escalate.
    if code == "internal_error":
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="server_bug",
                       action=f"Email contact@genomicintelligence.ai with request_id={request_id}.")
    if code in ("http_error", "unknown"):
        raise APIError(response.status_code, code, msg, request_id, details,
                       bucket="server_bug",
                       action="Generic failure the service could not classify. Capture request_id.")

    # Unknown code — the enum is closed today (21 values) but a new code can
    # ship before your client is regenerated, so treat an unlisted value as a
    # generic failure rather than a parse error. Log loudly.
    raise APIError(response.status_code, code, msg, request_id, details,
                   bucket="server_bug",
                   action=f"Unknown error.code={code!r}. Treat as server bug; capture and escalate.")


def demo() -> int:
    if not API_KEY:
        print("ERROR: set GI_API_KEY"); return 2

    # Trigger 422 validation_failed. "ZZZZ" is both out-of-alphabet and below
    # promoter's 300 bp floor (each task publishes its own minLength: splice
    # 100, enhancer 50, chromatin 200, annotation 1000, expression 9198 with the TSS at least 4599 from each end).
    # Either way the code is validation_failed — which is exactly why you
    # switch on `code` and not on the shape of `details`.
    print("# trigger validation_failed (422)")
    r = requests.post(
        f"{BASE_URL}/v1/tasks/promoter/predict",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"sequence": "ZZZZ", "sequence_name": "bad-input"},
        timeout=30,
    )
    try:
        handle(r)
    except APIError as exc:
        print(f"  caught: {exc.code} bucket={exc.bucket}")
        print(f"  action: {exc.action}")
        print(f"  message: {exc.message}")

    # Trigger 401 unauthorized with a clearly-bad key.
    print("\n# trigger unauthorized (401)")
    r = requests.get(
        f"{BASE_URL}/v1/tasks/promoter/models",
        headers={"Authorization": "Bearer gi_definitely_not_a_real_key"},
        timeout=10,
    )
    try:
        handle(r)
    except APIError as exc:
        print(f"  caught: {exc.code} bucket={exc.bucket}")
        print(f"  action: {exc.action}")
        print(f"  www-authenticate: {r.headers.get('WWW-Authenticate')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(demo())
