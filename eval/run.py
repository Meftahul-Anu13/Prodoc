"""Measure the offline pipeline on the held-out synthetic test split only.

No tuning, external requests, queue timing, LLM judge, cost estimates, or charts.
Each processing attempt is timed with perf_counter, including failed OCR attempts.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import time
import unicodedata

from .generate import FIELDS

ITEM_FIELDS = ("description", "quantity", "unit_price", "line_total")
NUMERIC = {"subtotal", "tax", "total", "quantity", "unit_price", "line_total"}


def normalized(value, field):
    if value is None or value == "":
        return None
    value = unicodedata.normalize("NFKC", str(value)).strip()
    if field in NUMERIC:
        try:
            result = Decimal(value.replace(",", ""))
            return str(result.normalize()) if result.is_finite() else value
        except InvalidOperation:
            return value
    if field == "invoice_date":
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                pass
    return " ".join(value.casefold().split())


def exact(value, expected):
    # Scalars are compared by their JSON text representation, not Python numeric equality.
    return json.dumps(value, sort_keys=True, ensure_ascii=False) == json.dumps(expected, sort_keys=True, ensure_ascii=False)


def rate(numerator, denominator):
    if not denominator:
        return {"numerator": numerator, "denominator": 0, "rate": None, "wilson95": None}
    p, z = numerator / denominator, 1.959963984540054
    base = 1 + z * z / denominator
    center = (p + z*z/(2*denominator)) / base
    margin = z * math.sqrt(p*(1-p)/denominator + z*z/(4*denominator*denominator)) / base
    return {"numerator": numerator, "denominator": denominator, "rate": p,
            "wilson95": [max(0, center-margin), min(1, center+margin)]}


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values)-1)*fraction
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi]-values[lo])*(position-lo)


def align(expected, predicted):
    """Match unique invoice IDs first; remaining records by original source order."""
    remaining = list(range(len(predicted)))
    matches = {}
    for i, gold in enumerate(expected):
        key = normalized(gold.get("invoice_number"), "invoice_number")
        candidates = [j for j in remaining if key is not None and normalized(predicted[j].get("invoice_number"), "invoice_number") == key]
        if len(candidates) == 1:
            matches[i] = candidates[0]
            remaining.remove(candidates[0])
    for i in range(len(expected)):
        if i not in matches and remaining:
            matches[i] = remaining.pop(0)
    return [(gold, predicted[matches[i]] if i in matches else None) for i, gold in enumerate(expected)] + [(None, predicted[j]) for j in remaining]


def score(doc, predictions, success):
    counts = defaultdict(int)
    per_field = {field: {"exact": 0, "normalized": 0, "denominator": 0} for field in FIELDS}
    errors = []
    for index, (gold, pred) in enumerate(align(doc["invoices"], predictions)):
        for field in FIELDS:
            expected = gold.get(field) if gold else None
            value = pred.get(field) if pred else None
            is_exact = bool(success and gold is not None and pred is not None and exact(value, expected))
            is_normal = bool(success and gold is not None and pred is not None and normalized(value, field) == normalized(expected, field))
            per_field[field]["denominator"] += 1
            per_field[field]["exact"] += is_exact
            per_field[field]["normalized"] += is_normal
            if not is_normal:
                errors.append({"invoice_index": index, "field": field, "expected": expected, "actual": value})
        gold_items = gold.get("line_items", []) if gold else []
        pred_items = pred.get("line_items", []) if pred else []
        for row in range(max(len(gold_items), len(pred_items))):
            old = gold_items[row] if row < len(gold_items) else None
            new = pred_items[row] if row < len(pred_items) else None
            row_exact = row_normal = bool(success and old is not None and new is not None)
            for field in ITEM_FIELDS:
                counts["item_fields"] += 1
                ex = bool(old is not None and new is not None and exact(new.get(field), old.get(field)))
                norm = bool(old is not None and new is not None and normalized(new.get(field), field) == normalized(old.get(field), field))
                counts["item_exact"] += ex
                counts["item_normalized"] += norm
                row_exact &= ex
                row_normal &= norm
            counts["item_rows"] += 1
            counts["row_exact"] += row_exact
            counts["row_normalized"] += row_normal
            if not row_normal:
                errors.append({"invoice_index": index, "field": "line_items", "row": row, "expected": old, "actual": new})
    # Operational proxy for unsupported assertions, not a claim about LLM hallucination:
    # a populated predicted value is supported iff it equals some visible gold value
    # of the same field anywhere in this document. Does not validate evidence spans.
    supported = defaultdict(set)
    for inv in doc["invoices"]:
        for field in FIELDS:
            supported[field].add(normalized(inv.get(field), field))
        for item in inv.get("line_items", []):
            for field in ITEM_FIELDS:
                supported["item."+field].add(normalized(item.get(field), field))
    for inv in predictions:
        entries = [(field, inv.get(field), field) for field in FIELDS]
        entries += [("item."+field, item.get(field), field) for item in inv.get("line_items", []) for field in ITEM_FIELDS]
        for key, value, field in entries:
            if value is not None and value != "":
                counts["assertions"] += 1
                counts["unsupported"] += normalized(value, field) not in supported[key]
    labeled_review = any(inv["needs_review"] for inv in doc["invoices"])
    safety_review = labeled_review or bool(errors)
    accepted = success and bool(predictions) and all(inv.get("needs_review") is False for inv in predictions)
    return {"fields": per_field, "counts": dict(counts), "errors": errors,
            "labeled_review": labeled_review, "safety_review": safety_review,
            "false_accept": bool(accepted and safety_review),
            "labeled_false_accept": bool(accepted and labeled_review),
            "reviewed": bool(success and any(inv.get("needs_review") is True for inv in predictions))}


def summarize(records):
    valid = [row for row in records if not row["expected_failure"]]
    corrupt = [row for row in records if row["expected_failure"]]
    aggregate = defaultdict(int)
    fields = {}
    for field in FIELDS:
        total = sum(row["score"]["fields"][field]["denominator"] for row in valid)
        fields[field] = {mode: rate(sum(row["score"]["fields"][field][mode] for row in valid), total) for mode in ("exact", "normalized")}
    for row in valid:
        for key, value in row["score"]["counts"].items():
            aggregate[key] += value
    duration = sum(row["elapsed_seconds"] for row in valid)
    total_slots = sum(fields[field]["exact"]["denominator"] for field in FIELDS)
    macro = {}
    for mode in ("exact", "normalized"):
        values = []
        for row in valid:
            denominator = sum(value["denominator"] for value in row["score"]["fields"].values())
            values.append(sum(value[mode] for value in row["score"]["fields"].values()) / denominator if denominator else 0)
        macro[mode] = {"mean_document_accuracy": statistics.mean(values) if values else None,
                       "documents": len(values), "confidence_interval": None}
    return {
        "valid_documents": len(valid), "expected_invoices": sum(row["expected_invoices"] for row in valid),
        "success": rate(sum(row["success"] for row in valid), len(valid)),
        "failure": rate(sum(not row["success"] for row in valid), len(valid)),
        "fields": fields,
        "all_scalar_fields": {mode: rate(sum(fields[field][mode]["numerator"] for field in FIELDS), total_slots) for mode in ("exact", "normalized")},
        "document_macro_scalar_accuracy": macro,
        "line_items": {"field_exact": rate(aggregate["item_exact"], aggregate["item_fields"]),
                       "field_normalized": rate(aggregate["item_normalized"], aggregate["item_fields"]),
                       "row_exact": rate(aggregate["row_exact"], aggregate["item_rows"]),
                       "row_normalized": rate(aggregate["row_normalized"], aggregate["item_rows"])},
        "ungrounded_value_proxy": rate(aggregate["unsupported"], aggregate["assertions"]),
        "false_accept_review": rate(sum(row["score"]["false_accept"] for row in valid), sum(row["score"]["safety_review"] for row in valid)),
        "false_accept_labeled_review": rate(sum(row["score"]["labeled_false_accept"] for row in valid), sum(row["score"]["labeled_review"] for row in valid)),
        "manual_review": rate(sum(row["score"]["reviewed"] for row in valid), sum(row["success"] for row in valid)),
        "known_corrupt": {"documents": len(corrupt), "correct_rejection": rate(sum(not row["success"] for row in corrupt), len(corrupt)), "unexpected_accepts": [row["id"] for row in corrupt if row["success"]]},
        "latency_seconds_valid_all_attempts": {"p50": percentile([row["elapsed_seconds"] for row in valid], .5), "p95": percentile([row["elapsed_seconds"] for row in valid], .95), "sum": duration},
        "sequential_throughput": {"attempted_documents_per_second": len(valid)/duration if duration else None,
                                  "successful_documents_per_second": sum(row["success"] for row in valid)/duration if duration else None,
                                  "expected_units_per_second": sum(row["expected_units"] for row in valid)/duration if duration else None,
                                  "label": "Single-process sequential direct pipeline calls; no queue/API/concurrent load benchmark. Units are pages/sheets, not CSV rows."},
    }


def display(metric):
    if metric["rate"] is None:
        return "N/A (0 denominator)"
    low, high = metric["wilson95"]
    return f"{metric['numerator']}/{metric['denominator']} = {metric['rate']:.2%} (Wilson95 {low:.2%}–{high:.2%})"


def markdown(report):
    summary = report["summary"]
    lines = ["# Measured held-out synthetic evaluation", "", f"Run: {report['timestamp_utc']}", "",
             "Offline deterministic local baseline only. Preliminary synthetic results, not LLM or real-world performance. Real-sample slice: **pending (0 samples)**. No test-driven tuning was performed by this harness.", "",
             "**Read the short-document slice and document-macro score before the micro average:** the optional 1,000-page fixture contributes 1,000 correlated invoices and can dominate field-weighted accuracy.", "",
             f"Short-document normalized scalar accuracy (excluding long_pdf): {display(report['without_long_pdf']['all_scalar_fields']['normalized'])}.",
             f"All-valid document-macro normalized scalar accuracy: {summary['document_macro_scalar_accuracy']['normalized']['mean_document_accuracy']:.1%} (each file equal weight; no binomial interval for a mean).", "",
             "## Definitions and limitations", ""]
    lines += [f"- {value}" for value in report["definitions"].values()]
    lines += ["", "## Overall results", ""]
    for key in ("success", "failure", "ungrounded_value_proxy", "false_accept_review", "false_accept_labeled_review", "manual_review"):
        lines.append(f"- {key}: {display(summary[key])}")
    lines += [f"- Scalar exact: {display(summary['all_scalar_fields']['exact'])}",
              f"- Scalar normalized: {display(summary['all_scalar_fields']['normalized'])}"]
    for key, value in summary["line_items"].items():
        lines.append(f"- Line items {key}: {display(value)}")
    lines += [f"- Known-corrupt correct rejection (separate denominator): {display(summary['known_corrupt']['correct_rejection'])}",
              f"- Latency seconds, valid attempts including failures: {json.dumps(summary['latency_seconds_valid_all_attempts'])}",
              f"- Sequential throughput: {json.dumps(summary['sequential_throughput'])}", "", "## Per-field results", "",
              "| Field | Exact | Normalized |", "|---|---|---|"]
    for field, values in summary["fields"].items():
        lines.append(f"| {field} | {display(values['exact'])} | {display(values['normalized'])} |")
    lines += ["", "## Per-format results", "", "| Format | Success | Scalar normalized | Item fields normalized |", "|---|---|---|---|"]
    for fmt, values in report["by_format"].items():
        lines.append(f"| {fmt} | {display(values['success'])} | {display(values['all_scalar_fields']['normalized'])} | {display(values['line_items']['field_normalized'])} |")
    lines += ["", "## Per-category results", "", "| Category | Success | Scalar normalized |", "|---|---|---|"]
    for category, values in report["by_category"].items():
        lines.append(f"| {category} | {display(values['success'])} | {display(values['all_scalar_fields']['normalized'])} |")
    lines += ["", "## Failures (including unavailable OCR, never skipped)", ""]
    lines += [f"- `{row['id']}`: {row['error']}" for row in report["documents"] if not row["success"]]
    lines += ["", "Full actual outputs, field differences, category breakdowns, timings, hashes, and denominators are in results.json.", ""]
    return "\n".join(lines)


def run(data: Path, output: Path):
    from backend.app.pipeline import process_file
    manifest_path = data / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    family_splits = defaultdict(set)
    for doc in manifest["documents"]:
        family_splits[doc["family"]].add(doc["split"])
    if any(len(splits) != 1 for splits in family_splits.values()):
        raise ValueError("Family leakage across splits")
    selected = [doc for doc in manifest["documents"] if doc["split"] == "test"]
    if not selected:
        raise ValueError("No held-out test documents")
    records = []
    wall_start = time.perf_counter()
    for doc in selected:
        path = (data / doc["path"]).resolve()
        if not path.is_relative_to(data.resolve()):
            raise ValueError("Fixture path escapes data directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
            raise ValueError(f"Fixture checksum mismatch: {doc['id']}")
        started = time.perf_counter()
        actual, error = None, None
        try:
            actual = process_file(path, path.name)
            if not isinstance(actual, dict) or not isinstance(actual.get("pages"), list) or not isinstance(actual.get("invoices"), list):
                raise ValueError("Invalid pipeline output contract")
            predictions = actual["invoices"]
            page_errors = [str(page.get("error")) for page in actual["pages"] if isinstance(page, dict) and page.get("error")]
            if page_errors or actual.get("metadata", {}).get("status") in ("failed", "partial"):
                raise ValueError("Pipeline failed/partial extraction: " + "; ".join(page_errors))
            if not predictions or not all(isinstance(inv, dict) and isinstance(inv.get("line_items", []), list) and all(isinstance(item, dict) for item in inv.get("line_items", [])) for inv in predictions):
                raise ValueError("No invoices or malformed invoice output")
            success = True
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            predictions, success = [], False
        elapsed = time.perf_counter() - started
        result = {key: doc[key] for key in ("id", "family", "format", "category", "expected_failure", "expected_units", "sha256")}
        result.update(success=success, elapsed_seconds=elapsed, expected_invoices=len(doc["invoices"]), actual=actual, error=error)
        if not doc["expected_failure"]:
            result["score"] = score(doc, predictions, success)
        records.append(result)
        print(f"{doc['id']}: {'ok' if success else 'FAILED'} {elapsed:.4f}s")
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "split": "test", "synthetic_only": True,
              "pipeline": "backend.app.pipeline.process_file; offline deterministic baseline",
              "environment": {"python": platform.python_version(), "platform": platform.platform()},
              "real_sample_slice": manifest["real_sample_slice"],
              "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "wall_seconds_including_harness": time.perf_counter()-wall_start,
              "definitions": {
                  "success": "Valid-document success means direct process_file returned a schema-shaped result with at least one invoice, no page errors and no failed/partial metadata status, not extraction correctness or job completion. Failures, including missing OCR and partial-page errors, stay in all valid denominators and receive no extraction credit. Known-corrupt cases are separate.",
                  "accuracy": "Eight scalar fields scored on all expected invoice slots, including explicit nulls; unmatched extra predicted invoices add incorrect slots. Failed/missing invoices get zero correct, even for null labels. Exact compares JSON representations; normalized uses NFKC, whitespace/case, common dates and Decimal for numeric fields. No fuzzy credit.",
                  "alignment": "Unique normalized invoice-number matches first, remaining invoices in source order. Line items align by row order, score description/quantity/unit_price/line_total and whole rows; extra or missing rows are incorrect slots.",
                  "ungrounded": "Unsupported-value proxy = populated predicted scalar/item values absent from same-field ground truth anywhere in that document / all populated predicted scalar/item values on valid documents. Tests source-value support, not evidence-span validity or hallucination provenance; wrong attribution can evade it. Not an LLM metric.",
                  "review": "False accept review = valid documents accepted with every predicted needs_review explicitly false despite a labeled missing/wrong-total issue OR an extraction mismatch / all valid documents with either condition (failed documents are not accepted). Labeled-only variant uses only labeled review-required documents. Manual review denominator is successful valid documents.",
                  "timing": "perf_counter elapsed for each real synchronous pipeline call, errors included. p50/p95 use linear interpolation. Throughput = valid attempts or successes divided by sum valid-call seconds. No queue wait, API, concurrency, memory, service-level, or 1000-page claims unless that fixture was actually included.",
                  "uncertainty": "95% Wilson binomial intervals on count rates. Small correlated synthetic format replicas/fields violate independence, so intervals are descriptive, not population guarantees. No latency confidence interval estimated.",
                  "scope": "Train/dev/test families are seed-isolated before rendering; this runner processes test only and does not tune anything. Template/layout diversity is limited. Labels are generator-derived/self-checked, not independently human reviewed. Real samples, provider/LLM accuracy, reconciliation, cost and queue-load benchmarks remain pending; no downloads or fabricated metrics.",
              },
              "summary": summarize(records),
              "without_long_pdf": summarize([row for row in records if row["category"] != "long_pdf"]),
              "by_format": {fmt: summarize([row for row in records if row["format"] == fmt]) for fmt in sorted({row["format"] for row in records})},
              "by_category": {category: summarize([row for row in records if row["category"] == category]) for category in sorted({row["category"] for row in records})},
              "documents": records}
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (output / "report.md").write_text(markdown(report), encoding="utf-8")
    print(f"Reports: {output / 'results.json'} and {output / 'report.md'}")
    print(f"Success {display(report['summary']['success'])}; normalized scalars {display(report['summary']['all_scalar_fields']['normalized'])}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("eval/data"))
    parser.add_argument("--output", type=Path, default=Path("eval/reports"))
    args = parser.parse_args()
    run(args.data, args.output)


if __name__ == "__main__":
    main()
