"""Genomic Intelligence API — partner quickstart.

Hits every public task endpoint with a real, biologically meaningful
sequence drawn from the bundled ``sequences/`` directory. The promoter,
splice, enhancer and chromatin files are the same fixtures the service uses
for its golden numeric-regression tests; the expression sample is not (it is
generated with ``Client.fetch_expression_window``, so its flank is the
largest ``recommended_flank_bp`` among the listed models). Each FASTA
header records its provenance.
Sync calls are sized so each task completes in a few seconds on a warm
GPU; ``annotation`` is the one outlier and is intentionally exercised
both sync and async. ``expression`` is the exception to the bundled
inputs: it fetches HBB's TSS window from Ensembl, sized from the live model
list, and falls back to the bundled fixture only when either is unreachable.

    pip install -r requirements.txt
    export GI_API_KEY=gi_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    export GI_BASE_URL=https://api.genomicintelligence.ai   # optional
    export GI_CLIENT_APP=my-tool                            # optional, sent as X-GI-Client-App
    python quickstart.py
"""

from __future__ import annotations

import os
import pathlib
import sys

import requests

from gi_client import (
    Client,
    EnsemblError,
    ExpressionWindowError,
    GIError,
    expression_flank_bp,
)


SEQ_DIR = pathlib.Path(__file__).parent / "sequences"


def load_fasta(filename: str) -> tuple[str, str]:
    """Return (sequence_name, sequence) from a single-record FASTA file."""
    text = (SEQ_DIR / filename).read_text()
    lines = text.splitlines()
    header = lines[0].lstrip(">").strip()
    sequence = "".join(line.strip() for line in lines[1:] if line.strip())
    return header, sequence.upper()


#: The bundled expression sample: HBB gene-sense, TSS-centred, with its
#: ``tss_index`` in the header. Used only when a live window cannot be built.
EXPRESSION_SAMPLE = "expression_hbb_k562.fa"


def load_expression_sample(filename: str = EXPRESSION_SAMPLE) -> tuple[str, str, int]:
    """Return (sequence_name, sequence, tss_index) for a TSS-centred sample.

    ``tss_index`` is read from the header's ``tss_index=<n>`` field, never
    assumed, so a regenerated sample cannot silently disagree with the
    index sent with it. ``sequence_name`` is the header's first four fields
    (species, gene, window, region), which stays inside the API's 128-char cap.
    """
    header, sequence = load_fasta(filename)
    fields = header.split("|")
    hits = [f.split("=", 1)[1] for f in fields if f.startswith("tss_index=")]
    if len(hits) != 1:
        raise ValueError(f"{filename}: header carries no single tss_index=<n> field: {header}")
    return "|".join(fields[:4]), sequence, int(hits[0])


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _summary(label: str, body: dict) -> None:
    meta = body.get("meta", {})
    counts = meta.get("task_specific_counts", {})
    print(
        f"{label:<22} model={meta.get('model'):<32} "
        f"{meta.get('inference_time_ms', '?')} ms  counts={counts}"
    )


def main() -> int:
    api_key = os.environ.get("GI_API_KEY")
    if not api_key:
        print("ERROR: set GI_API_KEY (your gi_… bearer key)", file=sys.stderr)
        return 2
    base_url = os.environ.get("GI_BASE_URL", "https://api.genomicintelligence.ai")

    # Optional attribution: unset, no X-GI-Client-App header is sent.
    client = Client(api_key=api_key, base_url=base_url,
                    client_app=os.environ.get("GI_CLIENT_APP") or None)

    _section("Health")
    print(client.health())

    # These four are real biological inputs from the service's golden
    # fixtures — see client/sequences/. Each FASTA carries
    # provenance in its header (gene symbol, coordinates, assembly).
    tp53_name,    tp53_seq    = load_fasta("promoter_tp53.fa")            # human, ~26 kb, gene-sense (TP53 is - strand)
    hbb_name,     hbb_seq     = load_fasta("splice_hbb.fa")               # human, ~4 kb
    eve_name,     eve_seq     = load_fasta("enhancer_eve.fa")             # drosophila, ~10 kb, eve developmental enhancer (+ strand)
    chr19_name,   chr19_seq   = load_fasta("chromatin_active_promoter_chr19.fa")  # human, ~40 kb

    _section("Sync inference (real biological sequences)")
    try:
        _summary("promoter (TP53)",
                 client.predict("promoter",  tp53_seq,  tp53_name))
        _summary("splice   (HBB)",
                 client.predict("splice",    hbb_seq,   hbb_name))
        # Enhancer model is Drosophila-trained — use a fly enhancer.
        _summary("enhancer (eve)",
                 client.predict("enhancer",  eve_seq,   eve_name))
        _summary("chromatin (chr19)",
                 client.predict("chromatin", chr19_seq, chr19_name))
    except GIError as exc:
        print(f"sync task failed: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        # Transport-level: DNS, TLS, connection refused, read timeout.
        # Routine for a hosted service and not a request problem.
        print(f"sync task failed: network error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _section("Expression (HBB in K562, TSS-centred)")
    # Expression reads the sequence around a gene's TSS. fetch_expression_window
    # sizes the window from the live model list (F = the largest
    # bio_spec.recommended_flank_bp, so one fetch serves every listed model),
    # takes the TSS from the Ensembl canonical transcript, reverse-complements
    # minus-strand genes such as HBB, and returns 2F bp gene-sense with the TSS
    # base at offset F and tss_index = F. Nothing here hard-codes a window, a
    # flank or a model id. Pass the model explicitly (the one your user named,
    # else default_model) so every call of a run uses the same one.
    #
    # If Ensembl or the model list is unreachable, the quickstart falls back to
    # the bundled sample: the same HBB window, generated with
    # fetch_expression_window, with its tss_index in the FASTA header. If the
    # model list answers but Ensembl does not, and the sample holds less than
    # 2F for the live F, it says so rather than scoring a short window
    # quietly. The number printed is the model's prediction for this exact
    # description: the description is model input, and rewording it changes
    # the score (see options.description on the Tasks page).
    model = None
    listing = None
    try:
        listing = client.list_models("expression")
        model = listing.get("default_model")
        window = client.fetch_expression_window("HBB", listing=listing)
        hbb_tss_seq, tss_index = window["sequence"], window["tss_index"]
        wmeta = window["meta"]
        hbb_tss_name = f"{wmeta['gene']}|{wmeta['region']}|strand={wmeta['strand']}"
        print(
            f"fetched {wmeta['gene']} ({wmeta['ensembl_id']}) {wmeta['region']} "
            f"strand {wmeta['strand']:+d}: {len(hbb_tss_seq):,} bp, "
            f"tss_index={tss_index} (flank from {wmeta['flank_model']})"
        )
    except (EnsemblError, ExpressionWindowError, GIError, requests.RequestException) as exc:
        hbb_tss_name, hbb_tss_seq, tss_index = load_expression_sample()
        print(
            f"note: could not build a live window ({type(exc).__name__}: {exc}); "
            f"using the bundled sample instead ({len(hbb_tss_seq):,} bp, "
            f"tss_index={tss_index})"
        )
        if listing is not None:
            live_flank, flank_model = expression_flank_bp(listing)
            if len(hbb_tss_seq) < 2 * live_flank:
                print(
                    f"note: the bundled sample is shorter than the model list "
                    f"recommends: {len(hbb_tss_seq):,} bp against "
                    f"{2 * live_flank:,} bp (2 x {live_flank:,} for {flank_model}); "
                    f"{model} will score less flank than it was trained on"
                )
    # tss_index is the 0-based offset into the whitespace-stripped sequence; an
    # index counted over line-wrapped FASTA characters is off by the newline
    # count and can silently score the wrong region, so check the
    # scored_window echoed back below.
    try:
        body = client.predict(
            "expression",
            sequence=hbb_tss_seq,
            sequence_name=hbb_tss_name,
            model=model,
            tss_index=tss_index,
            options={
                "description": (
                    "assay term name is polyA plus RNA-seq. "
                    "biosample summary is Homo sapiens K562."
                ),
            },
        )
        pred = body.get("data", {}).get("prediction", {})
        counts = body.get("meta", {}).get("task_specific_counts", {})
        print(
            f"expression: {pred.get('expression_log_tpm')} log(TPM+1)  "
            f"({pred.get('expression_tpm')} TPM)"
        )
        # Windowing provenance: confirm the server scored the window you meant.
        print(
            f"  tss_index={counts.get('tss_index')}  "
            f"scored_window={counts.get('scored_window')}"
        )
    except GIError as exc:
        print(f"expression failed: {exc}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"expression failed: network error: {type(exc).__name__}: {exc}", file=sys.stderr)

    _section("Async job — annotation on TP53 (~26 kb)")
    # Annotation is the slowest atomic task and the one where async
    # actually matters. TP53 is well-annotated; expect at least one
    # transcript in the response.
    try:
        job_id = client.submit_async(
            "annotation",
            sequence=tp53_seq,
            sequence_name=tp53_name,
        )
        print(f"submitted job_id={job_id}")

        def progress(p):
            pct = p.get("current_percent")
            msg = p.get("message", "")
            print(f"  {pct:>3}% {msg}")

        body = client.wait_for_job(job_id, poll_interval=2.0, on_progress=progress)
        meta = body.get("meta", {})
        counts = meta.get("task_specific_counts", {})
        transcripts = body.get("data", {}).get("transcripts", []) or []
        print(
            f"done — counts={counts}  "
            f"transcripts={len(transcripts)}  "
            f"total_time_ms={meta.get('inference_time_ms')}"
        )
    except GIError as exc:
        print(f"annotation failed: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"annotation failed: network error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except TimeoutError as exc:
        # wait_for_job gives up rather than polling forever.
        print(f"annotation timed out: {exc}", file=sys.stderr)
        return 1

    _section("Recent jobs")
    print(client.list_jobs(limit=5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
