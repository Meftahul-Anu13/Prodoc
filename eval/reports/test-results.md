# Test results

Run on 2026-10-31 on Windows 11, Python 3.13.5, Node 22.17.1.

| Check | Result |
|---|---|
| Backend and evaluation tests | **264 passed** |
| Backend coverage | **80%** of `backend/app` and `eval` |
| Frontend tests | **18 passed** |
| TypeScript and production build | Passed |
| Clean install from committed files only | Passed: 263 passed, 1 skipped, 18 frontend tests, build |
| Python dependency audit (100 packages) | No known vulnerabilities |
| npm production dependency audit | 0 vulnerabilities |
| Real Chrome check, desktop and phone width | Passed: no page errors, no failed requests, no sideways scrolling |

The clean-install skip is the baseline OCR test. It needs separately provisioned RapidOCR models, and tests never download models.

The Chrome check covered login, documents, review, evidence, findings, pages, audit, search and reconciliation screens against a copy of the real database. Semantic search was switched off for that check, and upload and correction weren't exercised in that browser run; they're covered by the automated tests.

Files: [tests.xml](tests.xml) (JUnit), [coverage.json](coverage.json), [dependency-audit.json](dependency-audit.json).

## What these tests don't prove

- Model responses and the MinerU SDK are mocked, so these tests prove behaviour rather than accuracy. Accuracy is in the [AI evaluation](ai/report.md).
- Coverage excludes the AI evaluation runner (`eval/run_ai.py`, 0%) and the CLI (`cli.py`, 0%), which were run by hand. `search.py` is at 62% because real vector search needs the embedding model.
- A scan is a point-in-time advisory check, not a warranty.
