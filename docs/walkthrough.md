# Walkthrough

**Recording:** [walkthrough.webm](walkthrough.webm) (2 min 10 s, on-screen captions, no voice).

It was driven automatically in real Chrome against the real API, worker, database and search index, using synthetic data. It shows live rejection of corrupted and irrelevant uploads, review, evidence, findings, a saved correction, audit history, typo-tolerant and semantic search, and cross-verification.

The model gateway was offline during recording, so the Northwind invoices shown were extracted by `gpt-6.1-sol` on 2026-10-29. The video doesn't show a new extraction running from start to finish.

The script below is for a narrated recording and takes about 6 minutes.

## Before recording

1. Follow [RUN.md](../RUN.md) and start the backend and frontend.
2. Check the LLM answers: `.\.venv\Scripts\python.exe -m scripts.ask_gateway "Reply OK"`
3. Create the sample: `.\.venv\Scripts\python.exe -m scripts.make_sample_invoice`
4. Generate the test files if `eval\data` is empty: `.\.venv\Scripts\python.exe -m eval.generate --output eval/data --long-pages 1000`
5. Use a fresh login or remove earlier documents, because identical files are recognised as duplicates.

## Script

| Time | Show | Say |
|---|---|---|
| 0:00 | README, architecture diagram | The problem, and the flow from upload to human review |
| 0:40 | Log in, upload the Northwind PDF plus a JPG, an XLSX and a CSV from `eval\data\test` | Multiple formats, and processing happens in the background |
| 1:20 | Upload queue and status changes | Jobs live in a durable queue, so they survive restarts |
| 2:00 | Open Northwind | Original on the left, fields on the right, two invoices found in one file |
| 2:40 | Click **Source: page 3** under total, then open Evidence | Each value is tied to its source text, so the model can't invent data |
| 3:10 | Findings tab | Validation issues, grouped by severity |
| 3:30 | Correct a value, enter a reason, save | Corrections are versioned |
| 3:50 | Audit tab, then finalize | Every action is recorded; finalized invoices are read-only |
| 4:10 | Search `Nortwind`, then `delivery charges` | Typo tolerance and semantic search, with the reason for each match |
| 4:40 | Reconciliation: pick the PDF and a spreadsheet invoice | Cross-verification, field by field |
| 5:10 | Upload `eval\data\test\corrupt.pdf` and a non-financial PDF | Failures are clear, and irrelevant files never reach the model |
| 5:30 | [AI report](../eval/reports/ai/report.md) | The measured results, including the 1000-page file, and their limits |

## Older recording

`eval/reports/browser/` holds an automated, non-narrated recording from 2026-10-28. It shows the old baseline and old UI, so it doesn't reflect the current app.
