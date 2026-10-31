# Requirements traceability

Each requirement mapped to its code and evidence. Test runs below are from 2026-10-31; the AI evaluation is from 2026-10-30.

**Status:** Done = implemented with evidence. Partial = implemented, with the gap stated.

## Tasks

| Requirement | Status | Implementation | Evidence |
|---|---|---|---|
| Accept PDF, JPG, PNG, TIFF, XLSX, XLS, CSV | Done | `main.py`, `sources.py` | All 7 formats in the evaluation |
| Digital PDF text, OCR for scans and images | Done | PyMuPDF text layer, MinerU 4.0.12 | Evaluation; `test_sources.py` |
| Multi-page PDFs, multiple sheets | Done | `sources.py`, `segmentation.py` | Multi-invoice family 64/64 fields |
| Poor image quality | Done | MinerU | `family-5-poor-scan.png` 7/8 fields, the only field error |
| Corrupted files | Done | Signature and structure checks | 7/7 rejected |
| PDFs of about 1000 pages | **Partial** | Segmentation, batching, cache | Reading and planning measured. **Full AI extraction never finished** (gateway outage). Long scans have no OCR checkpoints |
| Extract 8 fields and line items as JSON | Done | `observation.py` | 99.60% fields, 99.19% line fields |
| No invented values | Done | Grounding nulls unsupported values | Adversarial tests; 1/487 unsupported-value proxy |
| Missing fields, bad numbers | Done | `financial.py` | Unit tests; `missing_field` family |
| Duplicate invoices | Done | `worker.py:add_duplicates`, hash check | API and worker tests |
| Subtotal, tax, line and total consistency | Done | `financial.py` | Unit tests; `wrong_total` family |
| Flag for manual review | Done | `needs_review`, findings | Every AI invoice requires review |
| REST APIs | Done | `main.py` | `test_api.py` |
| Background OCR and AI | Done | `worker.py` durable queue | Queue and recovery tests |
| Database for metadata, text, results, errors, history | Done | `db.py` | See [schema](architecture.md#database-schema) |
| Keyword filtering | Done | `textsearch.py` FTS5, typo tolerance | API tests |
| Semantic search | Done | `search.py`, Qdrant, BGE | Live checks in embedded mode; retrieval accuracy not benchmarked |
| React UI for the full workflow | Done | `frontend/src/App.tsx` | 19 tests; real Chrome check on 2026-10-31, including phone width, and a [recorded walkthrough](walkthrough.md) |
| Automated tests for all listed areas | Done | `backend/tests`, `eval/test_eval.py`, `frontend/src` | [Test results](../eval/reports/test-results.md) |
| Labeled evaluation with measured metrics | **Partial** | `eval/run_ai.py` | [Report](../eval/reports/ai/report.md). 27 valid and 7 corrupt files measured; 1000-page and Northwind files not evaluated |

## Cross-verification

| Requirement | Status | Evidence |
|---|---|---|
| PDF to PDF and PDF to Excel | Partial | `reconciliation` API and UI compare a pair the user selects, with tests. Automatic pair finding isn't built, and accuracy isn't benchmarked |

## Quality goals

| Goal | Status | Evidence |
|---|---|---|
| Performance | Partial | p50 49.4 s, p95 87.4 s per document, sequential. Digital reads are near-instant; most time is OCR and model calls |
| Scalability | Partial | Designed for it (leases, batching, cache). **No load test.** See the [scaling path](architecture.md#scaling-path) |
| Reliability | Done | Durable jobs, restart-safe leases, resumable extraction, outages reported rather than hidden |
| Security | Done for local use | [SECURITY.md](../SECURITY.md). Not production-certified |

## Assumptions

- Reviewers see their own documents; admins see all.
- There is no currency conversion. Tolerance is one minor unit of the invoice currency.
- Human corrections are assertions and aren't re-checked against the source.
- Evaluation data is synthetic. No real supplier documents were used.
