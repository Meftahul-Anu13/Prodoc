# Test results

Run on 2026-10-31 on Windows 11, Python 3.13.5, Node 22.17.1.

| Check | Result |
|---|---|
| Backend and evaluation tests | **269 passed** |
| Backend coverage | **80%** of `backend/app` and `eval` |
| Frontend tests | **19 passed** |
| TypeScript and production build | Passed |
| Clean install from committed files only | Passed: 263 passed, 1 skipped, 18 frontend tests, build |
| Python dependency audit (100 packages) | No known vulnerabilities |
| npm production dependency audit | 0 vulnerabilities |
| Real Chrome check, desktop and phone width | Passed: no page errors, no failed requests, no sideways scrolling |

The clean-install skip is the baseline OCR test. It needs separately provisioned RapidOCR models, and tests never download models.

The Chrome check covered login, documents, review, evidence, findings, pages, audit, search and reconciliation screens against a copy of the real database. The [recorded walkthrough](../../docs/walkthrough.md) then ran rejected uploads, a saved correction, semantic search and reconciliation in Chrome. It found three UI bugs, all fixed: a missing save confirmation, the original document scrolling out of view, and a misleading message on rejected files.

The clean install ran before those fixes and before the gateway retry tests were added, so it counted 263 backend and 18 frontend tests.

Running the 1000-page evaluation found two more bugs, both fixed and tested: 12-invoice batches exceeded the gateway's time limit, and one temporary proxy error (HTTP 408) ended a whole run. The gateway client now retries temporary errors and stops at once when the account's quota is spent.

Files: [tests.xml](tests.xml) (JUnit), [coverage.json](coverage.json), [dependency-audit.json](dependency-audit.json).

## What these tests don't prove

- Model responses and the MinerU SDK are mocked, so these tests prove behaviour rather than accuracy. Accuracy is in the [AI evaluation](ai/report.md).
- Coverage excludes the AI evaluation runner (`eval/run_ai.py`, 0%) and the CLI (`cli.py`, 0%), which were run by hand. `search.py` is at 62% because real vector search needs the embedding model.
- A scan is a point-in-time advisory check, not a warranty.
