"""Evaluate the current AI pipeline on the same held-out test split as the baseline.

Runs backend.app.parse_job.process_document (source reading -> guardrail ->
segmentation -> observe/extract via the configured provider -> grounding) on
every test document, sequentially so per-document timings are not distorted
by concurrency. Scoring, alignment, Wilson intervals and report sections are
reused unchanged from eval/run.py, so the numbers compare directly with the
baseline report. Model responses are cached by content hash: an interrupted
run resumes, and re-runs cost nothing.

    .venv-mineru\\Scripts\\python.exe -m eval.run_ai [--skip-long] [--extra var/samples]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .run import display, markdown, score, summarize

ROOT = Path(__file__).resolve().parents[1]


def configure(provider):
    os.environ.update({
        "EXTRACTION_PROVIDER": provider, "GUARDRAIL_RELEVANCE": "enforce", "PDF_TEXT_LAYER": "auto",
        "EXTRACTION_CACHE": "on", "EXTRACTION_CACHE_DIR": str(ROOT / "eval" / ".cache" / provider),
        "MINERU_HOME": str(ROOT / "var" / "mineru"), "MINERU_TIER": "basic", "MINERU_MODEL_SOURCE": "local",
        "MINERU_MODEL_BASE_DIR": str(ROOT / "var" / "mineru" / "models"), "MINERU_MODEL_SMALL_BACKEND": "onnx",
    })


def usage(metadata):
    runs = metadata.get("extraction_runs") or []
    tokens_in = sum((run.get("usage") or {}).get("input_tokens", 0) for run in runs)
    tokens_out = sum((run.get("usage") or {}).get("output_tokens", 0) for run in runs)
    return {"model_calls": len(runs), "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cache": "hit" if metadata.get("cache") == "hit" or (runs == [] and metadata.get("segmentation")) else "miss"}


def evaluate(doc, path, work):
    from backend.app.parse_job import process_document
    started = time.perf_counter()
    actual, error, predictions, success = None, None, [], False
    try:
        actual = process_document(path, path.name, Path(tempfile.mkdtemp(dir=work)))
        predictions = actual["invoices"]
        page_errors = [str(p.get("error")) for p in actual["pages"] if isinstance(p, dict) and p.get("error")]
        if page_errors:
            raise ValueError("Page errors: " + "; ".join(page_errors))
        if not predictions:
            raise ValueError("No invoices extracted")
        success = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {getattr(exc, 'code', exc)}"
        predictions = []
    elapsed = time.perf_counter() - started
    metadata = (actual or {}).get("metadata", {})
    record = {key: doc.get(key) for key in ("id", "family", "format", "category", "expected_failure", "expected_units", "sha256")}
    record.update(success=success, elapsed_seconds=elapsed, expected_invoices=len(doc["invoices"]), error=error,
                  predicted_invoices=len(predictions), usage=usage(metadata),
                  reader=metadata.get("reader"), guardrail=(metadata.get("guardrail") or {}).get("verdict"),
                  review_flags=metadata.get("review_flags", []), segmentation=metadata.get("segmentation"),
                  finalization_blocked=metadata.get("finalization_blocked"),
                  predictions=[{k: v for k, v in inv.items() if k in ("invoice_number", "invoice_date", "supplier_name", "customer_name",
                                                                         "currency", "subtotal", "tax", "total", "line_items", "needs_review")}
                               for inv in predictions])
    if not doc["expected_failure"]:
        record["score"] = score(doc, predictions, success)
    return record


def _pages(path):
    import fitz
    with fitz.open(path) as document:
        return document.page_count


OUTAGE = ("unavailable", "connection_failed", "rate_limited")


def outage(record):
    """Provider outages are infrastructure failures, not extraction results."""
    return any(marker in (record.get("error") or "") for marker in OUTAGE)


def extra_documents(folder):
    """Labeled extra fixtures: <name>.pdf with <name>.answer-key.json next to it."""
    docs = []
    for key in sorted(Path(folder).glob("*.answer-key.json")):
        source = key.with_name(key.name.replace(".answer-key.json", ".pdf"))
        if source.is_file():
            invoices = [{**{k: v for k, v in inv.items() if k != "pages"}, "needs_review": False}
                        for inv in json.loads(key.read_text(encoding="utf-8"))]
            docs.append(({"id": f"extra/{source.name}", "family": "extra", "format": "pdf", "category": "multi_page_pack",
                          "expected_failure": False, "expected_units": _pages(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                          "invoices": invoices}, source))
    return docs


def ai_markdown(report, baseline):
    summary, lines = report["summary"], []
    lines += ["# AI pipeline evaluation (held-out test split)", "",
              f"Run: {report['timestamp_utc']} · provider `{report['provider']}` · model `{report['model']}` · reader routing: PDF text layer when every page has one, otherwise local MinerU 4.0.12 Basic/ONNX.", "",
              "Same 35-document held-out test split, scoring code and denominators as the baseline report "
              "(`eval/run.py`), so the two are directly comparable. Synthetic, generator-labeled documents: "
              "preliminary, not real-world accuracy. No prompt or threshold was tuned on this split.", "",
              "## Baseline vs AI pipeline", "", "| Metric | Baseline (heuristic) | AI pipeline |", "|---|---|---|"]
    rows = [("Valid-document success", lambda s: s["success"]),
            ("Scalar fields, normalized (all valid)", lambda s: s["all_scalar_fields"]["normalized"]),
            ("Line-item fields, normalized", lambda s: s["line_items"]["field_normalized"]),
            ("Line-item rows, normalized", lambda s: s["line_items"]["row_normalized"]),
            ("Unsupported-value proxy (lower is better)", lambda s: s["ungrounded_value_proxy"]),
            ("Known-corrupt correctly rejected", lambda s: s["known_corrupt"]["correct_rejection"])]
    for label, pick in rows:
        lines.append(f"| {label} | {display(pick(baseline['summary'])) if baseline else 'n/a'} | {display(pick(summary))} |")
    short_b = baseline["without_long_pdf"]["all_scalar_fields"]["normalized"] if baseline else None
    lines.append(f"| Scalar normalized, excluding the 1000-page file | {display(short_b) if short_b else 'n/a'} | {display(report['without_long_pdf']['all_scalar_fields']['normalized'])} |")
    latency = summary["latency_seconds_valid_all_attempts"]
    if report["not_evaluated_provider_outage"]:
        lines += ["", "**Not evaluated (model provider outage during the run, excluded from every rate above):** "
                  + ", ".join(f"`{d}`" for d in report["not_evaluated_provider_outage"]) + ". Re-run with `--resume` when the provider is available."]
    lines += ["", "## Processing time and cost", "",
              f"- Per-document latency (valid documents, sequential, includes parsing + model calls; cached documents excluded below): p50 {latency['p50']:.1f}s, p95 {latency['p95']:.1f}s, total {latency['sum']:.0f}s.",
              f"- Model calls: {report['usage']['model_calls']}, input tokens {report['usage']['input_tokens']:,}, output tokens {report['usage']['output_tokens']:,}.",
              f"- Documents answered from the content-hash cache in this run: {report['usage']['cached_documents']} (their latency reflects the cache, not the model).", "",
              "## Per-document results", "", "| Document | Reader | Success | Invoices (exp/pred) | Scalar normalized | Seconds | Calls | Flags / error |", "|---|---|---|---|---|---|---|---|"]
    for row in report["documents"]:
        scalar = "—"
        if row.get("score"):
            fields = row["score"]["fields"].values()
            denominator = sum(f["denominator"] for f in fields)
            scalar = f"{sum(f['normalized'] for f in fields)}/{denominator}" if denominator else "—"
        note = row["error"] or ", ".join(row["review_flags"]) or ("blocked: review" if row["finalization_blocked"] else "")
        lines.append(f"| `{row['id']}` | {row['reader'] or '—'} | {'yes' if row['success'] else 'no'} | {row['expected_invoices']}/{row['predicted_invoices']} | {scalar} | {row['elapsed_seconds']:.1f} | {row['usage']['model_calls']} | {note} |")
    lines += ["", "---", "", "Standard sections below are generated by the shared baseline report code.", ""]
    standard = markdown(report).split("\n")
    # Replace the baseline-specific heading/disclaimer with ours; keep definitions and tables.
    start = next(i for i, line in enumerate(standard) if line.startswith("## Definitions"))
    return "\n".join(lines + standard[start:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "eval" / "data")
    parser.add_argument("--output", type=Path, default=ROOT / "eval" / "reports" / "ai")
    parser.add_argument("--provider", default="gateway", choices=["gateway", "local", "anthropic"])
    parser.add_argument("--skip-long", action="store_true", help="skip the 1000-page fixture")
    parser.add_argument("--only", default="", help="comma-separated substrings of document ids to run")
    parser.add_argument("--resume", action="store_true", help="keep finished results from progress.json; rerun only provider outages")
    parser.add_argument("--extra", type=Path, default=ROOT / "var" / "samples", help="folder with extra labeled PDFs")
    args = parser.parse_args()
    configure(args.provider)
    manifest_path = args.data / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = [(doc, (args.data / doc["path"]).resolve()) for doc in manifest["documents"] if doc["split"] == "test"]
    if args.skip_long:
        selected = [(doc, path) for doc, path in selected if doc["category"] != "long_pdf"]
    # The long document goes last so every short result is available early.
    selected.sort(key=lambda item: item[0]["category"] == "long_pdf")
    if args.extra and args.extra.is_dir():
        selected += extra_documents(args.extra)
    if args.only:
        wanted = [w for w in args.only.split(",") if w]
        selected = [(doc, path) for doc, path in selected if any(w in doc["id"] for w in wanted)]
    for doc, path in selected:
        if doc["family"] != "extra" and hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
            raise ValueError(f"Fixture checksum mismatch: {doc['id']}")
    work = Path(tempfile.mkdtemp(prefix="eval-ai-"))
    records, wall = [], time.perf_counter()
    if args.resume and (args.output / "progress.json").is_file():
        kept = [r for r in json.loads((args.output / "progress.json").read_text(encoding="utf-8")) if not outage(r)]
        done = {r["id"] for r in kept}
        records = kept
        selected = [(doc, path) for doc, path in selected if doc["id"] not in done]
        print(f"Resuming: {len(kept)} finished documents kept, {len(selected)} to run", flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    for number, (doc, path) in enumerate(selected, 1):
        record = evaluate(doc, path, work)
        records.append(record)
        state = "ok" if record["success"] else "FAILED"
        print(f"[{number}/{len(selected)}] {doc['id']}: {state} {record['elapsed_seconds']:.1f}s calls={record['usage']['model_calls']} {record['error'] or ''}", flush=True)
        (args.output / "progress.json").write_text(json.dumps(records, indent=1, default=str), encoding="utf-8")
        # A dead provider would otherwise turn every remaining document into a
        # misleading "extraction failure". Stop instead; cached work resumes on rerun.
        recent = [r["error"] or "" for r in records[-3:]]
        if len(recent) == 3 and all("unavailable" in e or "connection_failed" in e for e in recent):
            raise SystemExit("Model provider unreachable for 3 documents in a row; no report written. "
                             "Restore the provider and rerun: finished documents come from the cache.")
    scored = [r for r in records if not outage(r)]
    try:
        from backend.app.gateway_llm import _settings
        model = _settings()[2] if args.provider == "gateway" else os.getenv("LOCAL_LLM_MODEL") or os.getenv("LLM_MODEL")
    except Exception:
        model = None
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "split": "test", "synthetic_only": True,
              "provider": args.provider, "model": model,
              "pipeline": "backend.app.parse_job.process_document (guardrail, segmentation, observe/extract, grounding)",
              "environment": {"python": platform.python_version(), "platform": platform.platform()},
              "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "wall_seconds_including_harness": time.perf_counter() - wall,
              "definitions": json.loads((ROOT / "eval" / "reports" / "results.json").read_text(encoding="utf-8"))["definitions"]
              if (ROOT / "eval" / "reports" / "results.json").is_file() else {},
              "usage": {"model_calls": sum(r["usage"]["model_calls"] for r in records),
                        "input_tokens": sum(r["usage"]["input_tokens"] for r in records),
                        "output_tokens": sum(r["usage"]["output_tokens"] for r in records),
                        "cached_documents": sum(1 for r in records if r["usage"]["cache"] == "hit")},
              "summary": summarize(scored),
              "without_long_pdf": summarize([r for r in scored if r["category"] != "long_pdf"]),
              "by_format": {f: summarize([r for r in scored if r["format"] == f]) for f in sorted({r["format"] for r in scored})},
              "by_category": {c: summarize([r for r in scored if r["category"] == c]) for c in sorted({r["category"] for r in scored})},
              "not_evaluated_provider_outage": [r["id"] for r in records if outage(r)],
              "documents": scored}
    report["definitions"] = {**report["definitions"],
                             "pipeline": "AI pipeline: success means process_document returned at least one invoice without page errors. Every AI invoice carries needs_review=true by design, so false-accept rates are 0 by construction, not by detection quality.",
                             "timing": "Sequential per-document wall time including source reading (text layer or MinerU OCR), guardrail, model calls and grounding. Remote model latency depends on the gateway; documents served from the cache are counted in usage.cached_documents."}
    baseline_path = ROOT / "eval" / "reports" / "results.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.is_file() else None
    (args.output / "results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (args.output / "report.md").write_text(ai_markdown(report, baseline), encoding="utf-8")
    print(f"Reports: {args.output / 'report.md'}")
    print(f"Success {display(report['summary']['success'])}; scalar normalized {display(report['summary']['all_scalar_fields']['normalized'])}")


if __name__ == "__main__":
    main()
