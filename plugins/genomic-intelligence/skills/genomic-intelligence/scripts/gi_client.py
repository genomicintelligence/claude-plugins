"""Tiny client for the Genomic Intelligence API.

Wraps `requests` with bearer auth, the unified `{data, meta}` /
`{error}` envelope, and a polling helper for async jobs. Drop this file
into your project and `from gi_client import Client`.

Endpoints covered:
  - the six per-task predictors, each its own published operation:
    ``POST /v1/tasks/{promoter,splice,enhancer,chromatin,annotation,
    expression}/predict``
  - the composite ``POST /v1/workflows/find-genes-and-predict-expression``
  - the async job endpoints ``GET /v1/tasks/jobs[/{job_id}]``
  - model discovery ``GET /v1/tasks/{task}/models``

Plus one input helper, ``Client.fetch_expression_window(symbol)``: a
TSS-centred, gene-sense expression input fetched from the public Ensembl REST
API, sized from the model list.

Contract reference: https://api.genomicintelligence.ai/redoc
"""

from __future__ import annotations

import time
from typing import Any, Dict, Mapping, Optional, Tuple

import requests


#: Minimum ``sequence`` length per task, in bp, measured AFTER whitespace is
#: stripped. This is a MIRROR for local pre-flight only — the authority is the
#: ``minLength`` published on each task's request schema in the live OpenAPI
#: document (``GET /v1/openapi.json``, public and unauthenticated), generated
#: from the service's ``api/core/limits.py``.
#:
#: The two can no longer disagree quietly. ``scripts/check_contract.py`` reads
#: this table straight out of this file and asserts it against
#: ``scripts/contract.json``, a pin of the live document; it runs on every PR
#: and **blocks the deploy**. ``scripts/sync_contract.py --check`` re-verifies
#: the pin against the live API daily. If you need to change a number here,
#: re-pin first and reconcile this table to the contract — never the reverse.
TASK_MIN_BP: Dict[str, int] = {
    "promoter": 300,
    "splice": 100,
    "enhancer": 50,
    "chromatin": 200,
    "annotation": 1_000,
    "expression": 9_198,
    "find-genes-and-predict-expression": 1_000,
}

#: Maximum ``sequence`` length, in bp, for every task (same mirror caveat).
#: Also published per model as ``bio_spec.request_max_bp``.
MAX_BP = 500_000

#: Above this, the composite workflow refuses synchronous delivery with
#: ``413 sync_too_large`` — resend the same body with ``Prefer: respond-async``.
FIND_GENES_SYNC_LIMIT_BP = 50_000

#: The expression floor per side: the TSS must sit at least this far from each
#: end. Derived from the pinned ``TASK_MIN_BP["expression"]`` (2 x 4,599) so
#: there is one number to keep in step with the contract, not two. It is a
#: floor, never the flank to fetch: :meth:`Client.fetch_expression_window`
#: takes the flank from the model list.
EXPRESSION_MIN_FLANK_BP = TASK_MIN_BP["expression"] // 2

#: Public Ensembl REST base (no key). Override per call with ``ensembl_url=``.
ENSEMBL_URL = "https://rest.ensembl.org"


class GIError(RuntimeError):
    """Raised on any non-2xx response from the API.

    Attributes mirror the unified error envelope so callers can switch
    on ``code`` rather than HTTP status alone:
        {"error": {"code": "...", "message": "...",
                   "request_id": "...", "details": ...}}

    ``details`` is carried through verbatim and never parsed. Its shape is
    keyed on the sibling ``code`` and has varied across releases — for
    ``validation_failed`` you may see either a bare ``[{loc, msg, type}, ...]``
    array or ``{"errors": [...]}`` — and splice's response-size cap emits a
    ``validation_failed`` with neither (``{task, record_count,
    maximum_records, sequence_length, threshold}``). Branch on ``code`` and
    treat ``details`` as display-only, and you are correct against all of them.
    """

    def __init__(
        self,
        status: int,
        body: Dict[str, Any],
        headers: Optional[Mapping[str, str]] = None,
    ):
        err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
        self.status = status
        self.code = err.get("code", "http_error")
        self.message = err.get("message", "")
        # The X-Request-Id header is set on every response, including any
        # error whose body omits error.request_id. Fall back to it — support
        # tickets always need a correlation id.
        self.request_id = err.get("request_id") or (headers or {}).get("X-Request-Id")
        self.details = err.get("details")
        rid = self.request_id or "unset"
        super().__init__(f"[{status} {self.code}] {self.message} (request_id={rid})")


class EnsemblError(RuntimeError):
    """Ensembl could not be reached, or did not resolve the gene or region."""


class ExpressionWindowError(ValueError):
    """The gene cannot be windowed for expression.

    Raised when the canonical TSS is so close to a chromosome end that even
    the 4,599 bp-per-side floor does not fit, or when Ensembl resolves no
    canonical transcript (the gene body can sit tens of kb from the TSS, so
    falling back to it would score the wrong window at full confidence).
    """


# ------------------------------------------------- expression window helpers
#
# Ported from a tested implementation; see the tests in
# kit/client/tests/test_expression_window.py. All genomic coordinates below
# are Ensembl's: 1-based, inclusive.


def expression_flank_bp(listing: Mapping[str, Any]) -> Tuple[int, Optional[str]]:
    """How much to fetch on each side of the TSS, and which model asked for it.

    ``listing`` is the body of :meth:`Client.list_models` for ``expression``.
    F is the largest ``bio_spec.recommended_flank_bp`` among the listed
    models, so one fetch serves whichever model scores it (the server ignores
    flank beyond what a model reads). It is never below the 4,599 bp floor and
    is capped so 2F fits every listed model's ``request_max_bp`` (or
    :data:`MAX_BP` where a model publishes none). Returns ``(F, model_id)``;
    ``model_id`` is None when no model recommends more than the floor.
    """
    models = [m for m in (listing.get("models") or []) if isinstance(m, dict)]
    caps = []
    for m in models:
        cap = (m.get("bio_spec") or {}).get("request_max_bp")
        caps.append(cap if isinstance(cap, int) and not isinstance(cap, bool) else MAX_BP)
    ceiling = (min(caps) if caps else MAX_BP) // 2
    best, source = EXPRESSION_MIN_FLANK_BP, None
    for m in models:
        f = (m.get("bio_spec") or {}).get("recommended_flank_bp")
        if not isinstance(f, int) or isinstance(f, bool):
            continue
        f = min(f, ceiling)
        if f > best or (f == best and source is None):
            best, source = f, m.get("id")
    return best, source


def expression_window_bounds(tss: int, strand: int, flank: int) -> Tuple[int, int]:
    """1-based inclusive genomic bounds of the 2F window around a 1-based TSS.

    The TSS must land at offset F of the sequence *as submitted*. Ensembl
    reverse-complements the region for ``strand=-1``, so that sequence reads
    from ``end`` down to ``start`` and the TSS sits at ``end - tss``; giving
    the extra base to the high side on the minus strand puts both strands at
    F. In 0-based terms: plus ``[tss-F, tss+F)``, minus ``(tss-F, tss+F]``
    then reverse-complemented. A one-base error here is invisible downstream:
    the length and the index are still legal, and the API scores a window
    shifted by one with full confidence.
    """
    if strand == -1:
        return tss - flank + 1, tss + flank
    return tss - flank, tss + flank - 1


def _largest_fitting_flank(tss: int, strand: int, flank: int, chrom_len: Optional[int]) -> int:
    """The largest symmetric flank <= ``flank`` whose window stays on the chromosome."""
    # Plus: start = tss - F >= 1, end = tss + F - 1 <= len.
    # Minus: start = tss - F + 1 >= 1, end = tss + F <= len.
    room_low = tss - 1 if strand == 1 else tss
    f = min(flank, room_low)
    if chrom_len is not None:
        room_high = chrom_len - tss + 1 if strand == 1 else chrom_len - tss
        f = min(f, room_high)
    return f


def _ensembl_get(base: str, path: str, *, accept: str = "application/json",
                 params: Optional[Dict[str, Any]] = None,
                 timeout: float = 30.0) -> requests.Response:
    # A bare requests.get, never the API session: the GI key must not go to Ensembl.
    try:
        return requests.get(
            f"{base.rstrip('/')}{path}",
            headers={"Accept": accept, "User-Agent": "gi-client"},
            params=params,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise EnsemblError(f"could not reach Ensembl at {base} ({type(exc).__name__}: {exc})") from exc


def lookup_symbol(symbol: str, species: str = "human",
                  ensembl_url: str = ENSEMBL_URL, timeout: float = 30.0) -> Dict[str, Any]:
    """Resolve a gene symbol with its canonical transcript (Ensembl ``expand=1``).

    Returns ``{ensembl_id, gene, seq_region, strand, tss}`` with ``tss`` the
    1-based canonical-transcript start on the plus strand, end on the minus
    strand. The gene body is never used as a fallback: its ends can sit tens
    of kb from the real TSS (HBB's gene end is 2,324 bp from it, ACTB's
    33,301 bp).
    """
    r = _ensembl_get(ensembl_url, f"/lookup/symbol/{species}/{symbol}",
                     params={"expand": 1}, timeout=timeout)
    if r.status_code == 404:
        raise EnsemblError(f"gene symbol {symbol!r} not found in {species}")
    if not r.ok:
        raise EnsemblError(
            f"Ensembl lookup failed ({r.status_code}) for {symbol!r} in species {species!r}"
        )
    try:
        d = r.json()
    except ValueError as exc:
        raise EnsemblError(f"Ensembl lookup for {symbol!r} returned non-JSON") from exc
    strand = -1 if int(d.get("strand", 1)) < 0 else 1
    canonical = next(
        (t for t in d.get("Transcript") or [] if t.get("is_canonical") == 1), None
    )
    if canonical is None:
        raise ExpressionWindowError(
            f"{symbol} ({d.get('id')}): Ensembl resolved no canonical transcript, so "
            "the TSS is unknown; supply the window and tss_index yourself"
        )
    tss = int(canonical["start"]) if strand == 1 else int(canonical["end"])
    return {
        "ensembl_id": d["id"],
        "gene": d.get("display_name", symbol),
        "seq_region": str(d["seq_region_name"]),
        "strand": strand,
        "tss": tss,
    }


def chromosome_length(seq_region: str, species: str = "human",
                      ensembl_url: str = ENSEMBL_URL, timeout: float = 30.0) -> Optional[int]:
    """Length of a seq region from Ensembl ``/info/assembly``; None if unknown."""
    r = _ensembl_get(ensembl_url, f"/info/assembly/{species}/{seq_region}", timeout=timeout)
    if not r.ok:
        return None
    try:
        length = r.json().get("length")
    except ValueError:
        return None
    return length if isinstance(length, int) and not isinstance(length, bool) else None


def fetch_region(seq_region: str, start: int, end: int, strand: int = 1,
                 species: str = "human", ensembl_url: str = ENSEMBL_URL,
                 timeout: float = 30.0) -> str:
    """Sequence of a 1-based inclusive region; ``strand=-1`` comes back reverse-complemented."""
    region = f"{seq_region}:{start}..{end}:{strand}"
    r = _ensembl_get(ensembl_url, f"/sequence/region/{species}/{region}",
                     accept="text/x-fasta", timeout=timeout)
    if not r.ok:
        raise EnsemblError(f"Ensembl sequence fetch failed ({r.status_code}) for {region}")
    return "".join(
        ln.strip() for ln in r.text.splitlines() if ln.strip() and not ln.startswith(">")
    ).upper()


class Client:
    """Thin synchronous client.

    >>> c = Client(api_key="gi_…")
    >>> r = c.predict("promoter", sequence="ACGT" * 500, sequence_name="demo")
    >>> r["meta"]["inference_time_ms"]
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.genomicintelligence.ai",
        timeout: float = 120.0,
        client_app: Optional[str] = None,
    ) -> None:
        """``client_app`` (optional) names the tool or group calling, sent as
        ``X-GI-Client-App`` on every request. It changes nothing about auth,
        routing or the response; it lets us tell callers sharing one key
        apart. Omitted, the header is not sent."""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        if client_app is not None:
            self._session.headers["X-GI-Client-App"] = client_app

    # ------------------------------------------------------------------ helpers

    def _check(self, resp: requests.Response) -> Dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            # A response arrived but is not JSON — usually an edge proxy. Use
            # http_error: error.code is a closed enum published in the schema,
            # so a synthesized value outside it is one no caller can match on.
            # Client-origin errors carry no request_id (the server never saw
            # this one), which is how you tell them apart from server errors.
            body = {"error": {"code": "http_error", "message": resp.text[:200]}}
        if not resp.ok:
            raise GIError(resp.status_code, body, resp.headers)
        return body

    # ----------------------------------------------------------------- requests

    def health(self) -> Dict[str, Any]:
        r = self._session.get(f"{self.base_url}/health", timeout=self.timeout)
        return self._check(r)

    def list_models(self, task: str) -> Dict[str, Any]:
        """``GET /v1/tasks/{task}/models`` — public; the key is sent but not needed.

        Returns a FLAT ``{task, default_model, models: [...]}`` object, not
        the ``{data, meta}`` envelope. Each model carries ``bio_spec`` with
        ``request_max_bp`` (the enforced ceiling), ``context_window_bp``
        (the model's own window — compare your sequence length against it to
        know whether the model saw real sequence or padding; null for
        annotation and expression) and ``trained_window_bp`` (fixed
        receptive field where the model has one; null for sliding-window
        models). Expression models also carry ``recommended_flank_bp``.
        Never hard-code a window, flank or model id: use the model your
        user named, else ``default_model``, and pass it as ``model`` on
        every call of a run; take F = the largest ``recommended_flank_bp``
        among the listed models (one fetch serves any of them), never below
        4,599 and with 2F within ``request_max_bp``. Put the TSS at offset F
        of the gene-sense sequence and send ``tss_index = F``. With ``tss``
        the 0-based genomic TSS position, fetch ``[tss - F, tss + F)`` on
        the plus strand, or ``(tss - F, tss + F]`` reverse-complemented on
        the minus strand. :meth:`fetch_expression_window` does all of this
        for a gene symbol.
        ``request_max_bp`` is the only cap; the window fields describe what
        the model reads, not what the route accepts.

        An unknown ``task`` is ``404 not_found``.
        """
        r = self._session.get(
            f"{self.base_url}/v1/tasks/{task}/models", timeout=self.timeout
        )
        return self._check(r)

    def fetch_expression_window(
        self,
        symbol: str,
        species: str = "human",
        listing: Optional[Mapping[str, Any]] = None,
        ensembl_url: str = ENSEMBL_URL,
    ) -> Dict[str, Any]:
        """Fetch a TSS-centred, gene-sense expression input for a gene symbol.

        Applies the rule in :meth:`list_models` end to end, so you need not:

        * F comes from ``list_models("expression")`` (or ``listing``, if you
          already hold it) via :func:`expression_flank_bp`: the largest
          ``recommended_flank_bp``, at least 4,599, 2F within
          ``request_max_bp``.
        * The TSS is the Ensembl canonical transcript's (its start on the
          plus strand, its end on the minus strand), never the gene body.
        * Minus-strand genes come back reverse-complemented, so the sequence
          is always gene-sense and the TSS base sits at offset F.
        * Near a chromosome end F shrinks to the largest symmetric flank that
          fits (``meta["flank_shortened"]``); below the 4,599 bp floor this
          raises :class:`ExpressionWindowError`.

        Returns ``{"sequence", "tss_index", "meta"}``. Send ``sequence`` and
        ``tss_index`` unchanged to :meth:`predict`, with ``model`` set to the
        model your user named or ``meta["default_model"]``::

            w = client.fetch_expression_window("HBB")
            client.predict("expression", w["sequence"], "HBB",
                           model=w["meta"]["default_model"],
                           tss_index=w["tss_index"],
                           options={"description": "..."})

        ``meta`` carries ``gene``, ``ensembl_id``, ``region``
        (``chrom:start-end``, 1-based inclusive), ``strand``, ``tss``
        (1-based), ``flank_bp``, ``flank_requested_bp``, ``flank_shortened``,
        ``flank_model`` (the model whose ``recommended_flank_bp`` set F;
        None if none asked for more than the floor), ``default_model`` and
        ``species``.

        Ensembl's public REST API is called directly (no key; your API key
        is never sent there), with the client's ``timeout`` per request. Raises :class:`EnsemblError` if Ensembl is
        unreachable or does not know the gene, and :class:`GIError` or a
        ``requests`` exception if the model list cannot be read.
        """
        if listing is None:
            listing = self.list_models("expression")
        requested, flank_model = expression_flank_bp(listing)
        locus = lookup_symbol(symbol, species, ensembl_url, self.timeout)
        tss, strand = locus["tss"], locus["strand"]
        chrom_len = chromosome_length(locus["seq_region"], species, ensembl_url, self.timeout)
        flank = _largest_fitting_flank(tss, strand, requested, chrom_len)
        if flank < EXPRESSION_MIN_FLANK_BP:
            raise ExpressionWindowError(
                f"{symbol}: the canonical TSS ({locus['seq_region']}:{tss:,}) is "
                f"{max(flank, 0):,} bp from a chromosome end, under the "
                f"{EXPRESSION_MIN_FLANK_BP:,} bp per side every expression model "
                f"needs ({TASK_MIN_BP['expression']:,} bp total), so it cannot be scored"
            )
        start, end = expression_window_bounds(tss, strand, flank)
        seq = fetch_region(locus["seq_region"], start, end, strand, species, ensembl_url,
                           self.timeout)
        if len(seq) != 2 * flank:
            raise EnsemblError(
                f"expected {2 * flank} bp for {locus['seq_region']}:{start}-{end}, "
                f"Ensembl returned {len(seq)}"
            )
        return {
            "sequence": seq,
            "tss_index": flank,
            "meta": {
                "gene": locus["gene"],
                "ensembl_id": locus["ensembl_id"],
                "region": f"{locus['seq_region']}:{start}-{end}",
                "strand": strand,
                "tss": tss,
                "flank_bp": flank,
                "flank_requested_bp": requested,
                "flank_shortened": flank < requested,
                "flank_model": flank_model,
                "default_model": listing.get("default_model"),
                "species": species,
            },
        }

    @staticmethod
    def _body(
        sequence: str,
        sequence_name: str,
        model: Optional[str],
        options: Optional[Dict[str, Any]],
        tss_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"sequence": sequence, "sequence_name": sequence_name}
        if model is not None:
            body["model"] = model
        if options is not None:
            body["options"] = options
        if tss_index is not None:
            body["tss_index"] = tss_index
        return body

    def predict(
        self,
        task: str,
        sequence: str,
        sequence_name: str = "sequence",
        model: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        tss_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Synchronous prediction. Returns the full ``{data, meta}`` body.

        ``task`` is one of ``promoter``, ``splice``, ``enhancer``,
        ``chromatin``, ``annotation``, ``expression`` — each a separately
        published operation at the same URL this method builds. An
        unrecognised task is ``404 not_found``, not a validation error.

        ``model`` is a TOP-LEVEL field, not ``options["model"]``: every
        task's ``options`` object is closed (``additionalProperties:
        false``), so an unknown key — ``model`` included — is rejected with
        ``422 validation_failed`` at ``loc ["body","options","<key>"]``.
        The accepted option keys differ per task; see the request schema in
        ReDoc (``PromoterOptions``, ``SpliceOptions``, …). ``expression``
        additionally *requires* ``options["description"]``.

        ``sequence`` must be within this task's bounds (see
        :data:`TASK_MIN_BP` / :data:`MAX_BP`, measured after whitespace is
        stripped); outside them is ``422 validation_failed``, never ``413``.
        A sequence above the floor but shorter than the selected model's
        ``bio_spec.context_window_bp`` (from :meth:`list_models`) is still
        accepted and scored — against a window padded out to that size.

        ``tss_index`` applies to the ``expression`` task only: the 0-based
        offset of the TSS into the whitespace-stripped ``sequence``. Every
        expression model takes at least 9,198 bp with the TSS at least
        4,599 bp from each end, i.e. ``4599 <= tss_index <= len(sequence) -
        4599``; ``tss_index`` is required unless ``sequence`` is exactly
        9,198 bp. 9,198 bp is a floor, not a target: longer TSS-centred
        sequence is better, and the model list says how much (the rule and
        strand handling are in :meth:`list_models`;
        :meth:`fetch_expression_window` builds the input from a gene
        symbol). There is no
        ``tes_index``. The client does not
        validate any of this — the API returns ``422 validation_failed``
        (the ``tss_index`` checks at ``loc ["body"]``, so switch on
        ``error.code``). A window dominated by ``N`` or low-complexity
        repeats can also be refused with ``422``: each model needs roughly
        2-3.3 kb of real sequence upstream of the TSS and 2.6 kb
        downstream.
        """
        body = self._body(sequence, sequence_name, model, options, tss_index)
        r = self._session.post(
            f"{self.base_url}/v1/tasks/{task}/predict",
            json=body,
            timeout=self.timeout,
        )
        return self._check(r)

    def submit_async(
        self,
        task: str,
        sequence: str,
        sequence_name: str = "sequence",
        model: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        tss_index: Optional[int] = None,
    ) -> str:
        """Submit a task in async mode. Returns the ``job_id``.

        ``Prefer: respond-async`` is a declared header parameter on all six
        predict operations and on the composite workflow. Async delivery is
        JSON-only: combining it with a text ``?format=`` is a ``400``.

        ``tss_index`` is the ``expression``-only TSS offset; see :meth:`predict`.
        """
        body = self._body(sequence, sequence_name, model, options, tss_index)
        r = self._session.post(
            f"{self.base_url}/v1/tasks/{task}/predict",
            headers={"Prefer": "respond-async"},
            json=body,
            timeout=self.timeout,
        )
        body = self._check(r)
        # 202 envelope is {data: {job_id, status, links}, meta: {...}} —
        # uniform with the {data, meta} shape every other successful
        # response on the inference URL produces.
        return body["data"]["job_id"]

    # --------------------------------------------------- composite workflow

    def find_genes_and_predict_expression(
        self,
        sequence: str,
        description: str,
        sequence_name: str = "sequence",
        options: Optional[Dict[str, Any]] = None,
        async_: bool = False,
    ) -> Dict[str, Any]:
        """``POST /v1/workflows/find-genes-and-predict-expression``.

        Runs annotation over the region, cuts a window around each found
        gene's TSS, and scores expression for each — one round trip. Each
        gene is scored with the API's default expression model (or the one
        named in ``options["expression_model"]``), using as much flank around
        each TSS as the submitted region holds, so longer regions give better
        predictions.

        ``sequence`` is 1,000–500,000 bp (the annotation stage's floor).
        ``description`` is the experimental context applied to every gene;
        it is required (missing or empty is ``422 validation_failed``) and
        is passed as ``options["description"]``. Extra ``options`` may add
        ``annotation_model``, ``expression_model`` and
        ``shift_coordinates``; the object is closed, so any
        other key is a ``422``.

        Above :data:`FIND_GENES_SYNC_LIMIT_BP` (50,000 bp) synchronous
        delivery is refused with ``413 sync_too_large`` carrying
        ``details = {sequence_length, threshold}`` — resend the identical
        body with ``async_=True``.

        Returns the ``{data, meta}`` body; with ``async_=True`` that body is
        the ``202`` acceptance ``{data: {job_id, status, links}, meta}``,
        so poll :meth:`wait_for_job` on ``body["data"]["job_id"]``.

        ``meta.task_specific_counts`` is
        ``{genes_found, genes_predicted, genes_skipped}`` with
        ``genes_predicted + genes_skipped == genes_found``; per-gene causes
        are in ``data.expression_predictions[].skip_reason``.
        """
        opts: Dict[str, Any] = dict(options or {})
        opts["description"] = description
        body = self._body(sequence, sequence_name, None, opts)
        headers = {"Prefer": "respond-async"} if async_ else None
        r = self._session.post(
            f"{self.base_url}/v1/workflows/find-genes-and-predict-expression",
            headers=headers,
            json=body,
            timeout=self.timeout,
        )
        return self._check(r)

    def get_job(self, job_id: str) -> requests.Response:
        """Single poll. The caller inspects ``status_code`` to discriminate."""
        return self._session.get(
            f"{self.base_url}/v1/tasks/jobs/{job_id}", timeout=self.timeout
        )

    def wait_for_job(
        self,
        job_id: str,
        poll_interval: float = 2.0,
        max_wait: float = 30 * 60,
        on_progress=None,
    ) -> Dict[str, Any]:
        """Poll until terminal. Returns ``{data, meta}`` on success, raises ``GIError``."""
        deadline = time.monotonic() + max_wait
        while True:
            r = self.get_job(job_id)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 202:
                if on_progress is not None:
                    try:
                        # 202 poll body is also {data: {progress: {...}}, meta}.
                        on_progress((r.json().get("data") or {}).get("progress") or {})
                    except Exception:
                        pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"job {job_id} did not finish within {max_wait}s")
                time.sleep(poll_interval)
                continue
            # Terminal error
            try:
                body = r.json()
            except ValueError:
                # http_error for the same reason as _check above: the code must
                # come from the published enum.
                body = {"error": {"code": "http_error", "message": r.text[:200]}}
            raise GIError(r.status_code, body, r.headers)

    def list_jobs(self, limit: int = 50) -> Dict[str, Any]:
        r = self._session.get(
            f"{self.base_url}/v1/tasks/jobs",
            params={"limit": limit},
            timeout=self.timeout,
        )
        return self._check(r)
