# Measured held-out synthetic evaluation

Run: 2026-10-28T13:51:01.082604+00:00

Offline deterministic local baseline only. Preliminary synthetic results, not LLM or real-world performance. Real-sample slice: **pending (0 samples)**. No test-driven tuning was performed by this harness.

**Read the short-document slice and document-macro score before the micro average:** the optional 1,000-page fixture contributes 1,000 correlated invoices and can dominate field-weighted accuracy.

Short-document normalized scalar accuracy (excluding long_pdf): 225/248 = 90.73% (Wilson95 86.47%–93.74%).
All-valid document-macro normalized scalar accuracy: 89.7% (each file equal weight; no binomial interval for a mean).

## Definitions and limitations

- Valid-document success means direct process_file returned a schema-shaped result with at least one invoice, no page errors and no failed/partial metadata status, not extraction correctness or job completion. Failures, including missing OCR and partial-page errors, stay in all valid denominators and receive no extraction credit. Known-corrupt cases are separate.
- Eight scalar fields scored on all expected invoice slots, including explicit nulls; unmatched extra predicted invoices add incorrect slots. Failed/missing invoices get zero correct, even for null labels. Exact compares JSON representations; normalized uses NFKC, whitespace/case, common dates and Decimal for numeric fields. No fuzzy credit.
- Unique normalized invoice-number matches first, remaining invoices in source order. Line items align by row order, score description/quantity/unit_price/line_total and whole rows; extra or missing rows are incorrect slots.
- Unsupported-value proxy = populated predicted scalar/item values absent from same-field ground truth anywhere in that document / all populated predicted scalar/item values on valid documents. Tests source-value support, not evidence-span validity or hallucination provenance; wrong attribution can evade it. Not an LLM metric.
- False accept review = valid documents accepted with every predicted needs_review explicitly false despite a labeled missing/wrong-total issue OR an extraction mismatch / all valid documents with either condition (failed documents are not accepted). Labeled-only variant uses only labeled review-required documents. Manual review denominator is successful valid documents.
- perf_counter elapsed for each real synchronous pipeline call, errors included. p50/p95 use linear interpolation. Throughput = valid attempts or successes divided by sum valid-call seconds. No queue wait, API, concurrency, memory, service-level, or 1000-page claims unless that fixture was actually included.
- 95% Wilson binomial intervals on count rates. Small correlated synthetic format replicas/fields violate independence, so intervals are descriptive, not population guarantees. No latency confidence interval estimated.
- Train/dev/test families are seed-isolated before rendering; this runner processes test only and does not tune anything. Template/layout diversity is limited. Labels are generator-derived/self-checked, not independently human reviewed. Real samples, provider/LLM accuracy, reconciliation, cost and queue-load benchmarks remain pending; no downloads or fabricated metrics.

## Overall results

- success: 28/28 = 100.00% (Wilson95 87.94%–100.00%)
- failure: 0/28 = 0.00% (Wilson95 0.00%–12.06%)
- ungrounded_value_proxy: 16/16407 = 0.10% (Wilson95 0.06%–0.16%)
- false_accept_review: 0/19 = 0.00% (Wilson95 0.00%–16.82%)
- false_accept_labeled_review: 0/14 = 0.00% (Wilson95 0.00%–21.53%)
- manual_review: 21/28 = 75.00% (Wilson95 56.64%–87.32%)
- Scalar exact: 8225/8248 = 99.72% (Wilson95 99.58%–99.81%)
- Scalar normalized: 8225/8248 = 99.72% (Wilson95 99.58%–99.81%)
- Line items field_exact: 6137/8248 = 74.41% (Wilson95 73.45%–75.34%)
- Line items field_normalized: 8173/8248 = 99.09% (Wilson95 98.86%–99.27%)
- Line items row_exact: 0/2062 = 0.00% (Wilson95 0.00%–0.19%)
- Line items row_normalized: 2036/2062 = 98.74% (Wilson95 98.16%–99.14%)
- Known-corrupt correct rejection (separate denominator): 7/7 = 100.00% (Wilson95 64.57%–100.00%)
- Latency seconds, valid attempts including failures: {"p50": 0.310172949999469, "p95": 6.333071944999025, "sum": 57.83259719999114}
- Sequential throughput: {"attempted_documents_per_second": 0.4841560185023869, "successful_documents_per_second": 0.4841560185023869, "expected_units_per_second": 17.82731625271289, "label": "Single-process sequential direct pipeline calls; no queue/API/concurrent load benchmark. Units are pages/sheets, not CSV rows."}

## Per-field results

| Field | Exact | Normalized |
|---|---|---|
| invoice_number | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) |
| invoice_date | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) |
| supplier_name | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) |
| customer_name | 1029/1031 = 99.81% (Wilson95 99.30%–99.95%) | 1029/1031 = 99.81% (Wilson95 99.30%–99.95%) |
| currency | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) |
| subtotal | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) |
| tax | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) |
| total | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) | 1028/1031 = 99.71% (Wilson95 99.15%–99.90%) |

## Per-format results

| Format | Success | Scalar normalized | Item fields normalized |
|---|---|---|---|
| csv | 3/3 = 100.00% (Wilson95 43.85%–100.00%) | 1/24 = 4.17% (Wilson95 0.74%–20.24%) | 0/24 = 0.00% (Wilson95 0.00%–13.80%) |
| jpg | 3/3 = 100.00% (Wilson95 43.85%–100.00%) | 24/24 = 100.00% (Wilson95 86.20%–100.00%) | 19/24 = 79.17% (Wilson95 59.53%–90.76%) |
| pdf | 6/6 = 100.00% (Wilson95 60.97%–100.00%) | 8048/8048 = 100.00% (Wilson95 99.95%–100.00%) | 8048/8048 = 100.00% (Wilson95 99.95%–100.00%) |
| png | 4/4 = 100.00% (Wilson95 51.01%–100.00%) | 32/32 = 100.00% (Wilson95 89.28%–100.00%) | 11/32 = 34.38% (Wilson95 20.41%–51.69%) |
| tiff | 4/4 = 100.00% (Wilson95 51.01%–100.00%) | 40/40 = 100.00% (Wilson95 91.24%–100.00%) | 15/40 = 37.50% (Wilson95 24.22%–52.97%) |
| xls | 4/4 = 100.00% (Wilson95 51.01%–100.00%) | 40/40 = 100.00% (Wilson95 91.24%–100.00%) | 40/40 = 100.00% (Wilson95 91.24%–100.00%) |
| xlsx | 4/4 = 100.00% (Wilson95 51.01%–100.00%) | 40/40 = 100.00% (Wilson95 91.24%–100.00%) | 40/40 = 100.00% (Wilson95 91.24%–100.00%) |

## Per-category results

| Category | Success | Scalar normalized |
|---|---|---|
| clean | 7/7 = 100.00% (Wilson95 64.57%–100.00%) | 48/56 = 85.71% (Wilson95 74.26%–92.58%) |
| corrupt | N/A (0 denominator) | N/A (0 denominator) |
| long_pdf | 1/1 = 100.00% (Wilson95 20.65%–100.00%) | 8000/8000 = 100.00% (Wilson95 99.95%–100.00%) |
| missing_field | 7/7 = 100.00% (Wilson95 64.57%–100.00%) | 49/56 = 87.50% (Wilson95 76.37%–93.81%) |
| multi_invoice | 4/4 = 100.00% (Wilson95 51.01%–100.00%) | 64/64 = 100.00% (Wilson95 94.34%–100.00%) |
| poor_scan | 1/1 = 100.00% (Wilson95 20.65%–100.00%) | 8/8 = 100.00% (Wilson95 67.56%–100.00%) |
| scanned | 1/1 = 100.00% (Wilson95 20.65%–100.00%) | 8/8 = 100.00% (Wilson95 67.56%–100.00%) |
| wrong_total | 7/7 = 100.00% (Wilson95 64.57%–100.00%) | 48/56 = 85.71% (Wilson95 74.26%–92.58%) |

## Failures (including unavailable OCR, never skipped)

- `test/corrupt.pdf`: PipelineError: CORRUPT_PDF: unable to open PDF
- `test/corrupt.jpg`: PipelineError: CORRUPT_IMAGE: invalid, oversized, or unreadable image
- `test/corrupt.png`: PipelineError: CORRUPT_IMAGE: invalid, oversized, or unreadable image
- `test/corrupt.tiff`: PipelineError: CORRUPT_IMAGE: invalid, oversized, or unreadable image
- `test/corrupt.xlsx`: PipelineError: CORRUPT_XLSX: unable to read workbook
- `test/corrupt.xls`: PipelineError: CORRUPT_XLS: unable to read legacy workbook
- `test/corrupt.csv`: PipelineError: CSV_ENCODING: input appears to be binary rather than CSV

Full actual outputs, field differences, category breakdowns, timings, hashes, and denominators are in results.json.
