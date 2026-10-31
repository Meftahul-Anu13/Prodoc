# Disposable pipeline experiment

**Test-only code. Nothing in the production app imports this folder.** Delete the whole folder when the experiment is over. It does not implement the production architecture.

## What this tests

Your objection is the reason for this experiment: **a workbook/CSV can have scattered labels, prose, multiple tables, repeated headers, vertical forms, transposed records or several rows per invoice. A fixed header dictionary is not enough.**

This lab does **not** use the application's existing header aliases, invoice-per-row extraction, Azure adapter, database or workers.

```text
PDF / images / DOCX / HTML -> optional local Docling or MinerU parsing --+
                                                                       |
CSV / XLSX / XLS -> all original nonempty cells + coordinates -----------+-> LLM layout observation
                                                                       |      -> LLM invoice extraction
TXT / Markdown -> original lines --------------------------------------+      -> source/Decimal checks
                                                                               -> human inspection
```

Native readers only recover source material. They do not decide where an invoice starts, which row is a header, what a column means, or how line items are grouped. **Both clean and messy spreadsheets go through the same LLM observation/extraction stages.** No threshold silently sends only easy examples to the model.

### Repository selected for unstructured documents

[Docling](https://github.com/docling-project/docling) is integrated as the optional document-to-Markdown/JSON parser. It supplies document reading order, text and table representation; the LLM then interprets business structure. This is a candidate, not a proven superior parser.

- Documentation: [local pipeline options](https://docling-project.github.io/docling/usage/advanced_options/), [formats](https://docling-project.github.io/docling/usage/supported_formats/).
- Code is MIT; selected model/dependency licenses are separate. Models may download on first conversion. Prefetch with `docling-tools models download`; `--artifacts-path` points to the prepared directory.
- Remote PDF services are explicitly disabled in this adapter. This is **not a verified no-egress sandbox**: bootstrap/model downloads and other converter behavior must be inspected. Use only synthetic/public fixtures until independently checked.
- Generic plain text is read directly; no model is needed just to decode text.
- CSV/XLSX/XLS deliberately retain original cells instead of taking a Markdown round trip. Their lack of business structure is handled by LLM observation, not an assumption that the file is a rectangular ledger.

A separate optional MinerU SDK adapter is also provided. It calls documented `mineru.parser.parse`, retains `to_dict()` and `markdown()`, and requires an explicit tier. Its runtime has **not** been installed or benchmarked by writing this harness. Use a release/profile you verify locally; 4.x and legacy APIs differ. Do not interpret a successful import as a successful OCR test. Do not install both heavyweight parsers into the same environment by default.

## Install in a separate environment

Run from the repository root in PowerShell. These commands create a new environment and do not change production dependencies:

```powershell
python -m venv pipeline_sandbox/.venv
./pipeline_sandbox/.venv/Scripts/python.exe -m pip install -r pipeline_sandbox/requirements.txt
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.make_samples
./pipeline_sandbox/.venv/Scripts/python.exe -m unittest discover -s pipeline_sandbox/tests -v
```

The sample generator refuses to overwrite an existing sample directory. Choose another directory with `--out pipeline_sandbox/samples/demo2` to regenerate. Optional Pillow installation adds synthetic PNG, scanned PDF and multi-frame TIFF samples; it is not needed for CSV/XLS/XLSX/text tests:

```powershell
./pipeline_sandbox/.venv/Scripts/python.exe -m pip install "Pillow==13.0.0"
# Use --out .../demo2 if samples/demo already exists.
```

For Docling, create a **second**, independently disposable environment (large dependencies/models):

```powershell
python -m venv pipeline_sandbox/.venv-docling
./pipeline_sandbox/.venv-docling/Scripts/python.exe -m pip install -r pipeline_sandbox/requirements-docling.txt
```

For MinerU, use a separate environment and install a release/profile verified against [the upstream SDK documentation](https://opendatalab.github.io/MinerU/usage/sdk_api/). `mineru==4.0.12` is the release investigated in the plan, not an installation result. Profile/backend selection, assets, hardware and license review remain your responsibility. The lab never falls back to a remote parser. To run offline, prepare assets first and configure the selected release's local-only/telemetry settings; a command-line flag alone is not proof of denied egress.

## 1. Inspect input locally: no LLM, no API charge

```powershell
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab prepare pipeline_sandbox/samples/demo/messy_invoice.csv
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab prepare pipeline_sandbox/samples/demo/mixed_layouts.xlsx
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab prepare pipeline_sandbox/samples/demo/scattered_invoice.xls
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab prepare pipeline_sandbox/samples/demo/unstructured_invoice.txt
```

For your own CSV, use an explicit override if detection is ambiguous:

```powershell
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab prepare "C:/your-files/problem.csv" --delimiter ";" --encoding cp1252
```

A unique directory under `pipeline_sandbox/runs/` is printed. Open `source.json`:

- Did all nonempty cells survive, including notes and hidden sheets?
- Are the delimiter, encoding, IDs, leading zeros and decimal tokens correct?
- Did the original locations survive? Is formula/cache uncertainty visible?

No invoice result is claimed by `prepare`. If source reading fails, fix that before testing interpretation. An LLM cannot reliably recover bytes/cells that were lost upstream.

## 2. Run actual LLM observation, then extraction

This sends source text/cells (including relevant sheet labels) to the configured Anthropic endpoint and incurs API charges. Set `ANTHROPIC_API_KEY` or supported SDK credentials securely in your shell; do not paste secrets into files or commit them. No `.env`, production credentials file or provider key is loaded by this lab. If `ANTHROPIC_BASE_URL` is configured, review where it sends data first.

The default model is `claude-opus-5-5`. SDK retries are disabled; server-side refusal fallbacks are enabled and can cause additional billed attempts. Usage and served model are recorded. The default is two logical requests, with up to 16,000 output tokens per request; this is a token ceiling, **not** a prepaid dollar budget. The lab does not execute API calls until you run `observe`/`run` with `--allow-cloud`.

```powershell
# One logical model request: see whether it understands the layout first.
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab observe pipeline_sandbox/samples/demo/mixed_layouts.xlsx --allow-cloud

# Two logical model requests: layout observation, then extraction using original evidence.
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab run pipeline_sandbox/samples/demo/messy_invoice.csv --allow-cloud
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab run pipeline_sandbox/samples/demo/transposed_invoices.csv --allow-cloud
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab run pipeline_sandbox/samples/demo/scattered_invoice.xls --allow-cloud
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab run pipeline_sandbox/samples/demo/unstructured_invoice.txt --allow-cloud

# Comma decimal separators are a deliberate test policy, not guessed from an amount.
./pipeline_sandbox/.venv/Scripts/python.exe -m pipeline_sandbox.lab run "C:/your-files/problem.csv" --delimiter ";" --decimal-style comma --allow-cloud
```

Each command starts a new run; `observe` followed by `run` repeats observation and is billed again. There is no hidden repair/retry loop. Observation records concise layout descriptions and uncertainties, not private model chain-of-thought.

### Document parser lane

```powershell
# Local parser test first. Models may need bootstrap downloads.
./pipeline_sandbox/.venv-docling/Scripts/python.exe -m pipeline_sandbox.lab prepare "C:/your-files/invoice.pdf" --parser docling --parse-timeout 600

# Then actual LLM interpretation, only after authorizing transmission.
./pipeline_sandbox/.venv-docling/Scripts/python.exe -m pipeline_sandbox.lab run "C:/your-files/invoice.pdf" --parser docling --parse-timeout 600 --allow-cloud

# In an independently prepared MinerU environment; tier name must match that release.
# python -m pipeline_sandbox.lab prepare "C:/your-files/invoice.pdf" --parser mineru --mineru-tier standard --parse-timeout 600
```

The sandbox's PDF/image evidence currently points to retained Markdown lines. **It does not claim original page/cell/bbox fidelity.** Original parser JSON is retained for inspection. This intentional limitation avoids fabricating geometry from an unknown parser schema. Verify actual page/frame coverage, especially TIFF and MinerU, before trusting a result. PDF Docling conversion is capped at 50 pages; this is not the 1000-page production test.

## Outputs to inspect

| Artifact | Meaning |
| --- | --- |
| `source.json` | Source blocks/locations, parser metadata, reader warnings; no schema interpretation |
| `parsed.md`, `parser_document.json` | Optional document parser artifacts |
| `observation.json` | Actual model-proposed layouts, regions, mappings and grouping uncertainties |
| `observation_coverage.json` | Source blocks the observation did not account for |
| `candidates.json` | Actual model proposals before deterministic grounding; never authoritative facts |
| `checked_results.json` | Source-grounded values, nulls, findings and manual-review status |
| `*_usage.json` | Actual provider usage, served model, request/stop metadata and timing |
| `*_response.txt` | Text response for debugging JSON/schema issues; no private thinking saved |
| `error.json` | Explicit failed run; a partial model response is not accepted as complete |

Compare with the synthetic `samples/demo/expected.json`, then test your real problematic files. Fixture labels are not model results. There is no hardcoded success score and no pretend LLM mode in the real CLI.

## What is and is not validated

- Each non-null candidate needs an existing source ID, a quotation in that block, and supporting raw text inside the quote. Numeric substrings and lost negative signs are rejected. Unsupported values become null.
- Source references must belong to the selected observed regions. This does **not** prove the model chose the correct region/column/business meaning.
- Monetary parsing uses Decimal and explicit `dot`/`comma` style; invalid/ambiguous strings require review. No inferred currency or absent adjustments default to zero. Dates and currency registry membership are not fully validated.
- Header arithmetic runs only when all required monetary terms and an explicit currency token are present. Line-net/tax basis is not assumed; line/subtotal differences remain manual-review observations. Tolerance is an explicit **test** policy, not production currency rules.
- **Every record remains `needs_review`, even if arithmetic agrees.** Grounding cannot distinguish a malicious but quoted instruction amount from a genuine total, nor prove OCR digits match the original. The injected-footer fixture is meant to expose this risk.
- Source/context limits fail visibly instead of silently truncating: 10 MiB input, 10,000 rows, 100,000 inspected cells, 80,000 context characters by default. Oversized contexts must be split deliberately; this harness does not solve cross-chunk grouping. Observation plus source is checked again before extraction.
- Parsing runs in a child process with a timeout. This is **not** a hardened sandbox, filesystem jail, memory limit or reliable descendant-process containment. Models/native parsers may allocate substantial RAM. Do not feed malicious/private documents into this local experiment without appropriate OS/container isolation.
- Worksheet formulas are never evaluated. XLSX numeric tokens come from original OOXML; formula caches remain explicitly unverified. XLS binary-number precision cannot be recovered. CSV readers preserve strings and coordinates; they do not repair truly malformed CSV automatically.
- No production reconciliation, search, UI, auth, storage lifecycle or 1000-page pipeline is implemented here. GenOffice is deliberately not added to this experiment: it would not resolve spreadsheet interpretation.

## Offline test boundary

```powershell
./pipeline_sandbox/.venv/Scripts/python.exe -m unittest discover -s pipeline_sandbox/tests -v
```

These tests exercise real CSV/XLS/XLSX fixtures, retained provenance, Decimal parsing and fabricated/ambiguous candidate rejection. The two-stage orchestration test uses an explicitly mocked caller and does **not** demonstrate real model accuracy.

The optional real Docling HTML test is skipped by default. Enable it only inside the Docling environment:

```powershell
$env:LAB_TEST_DOCLING = "1"
./pipeline_sandbox/.venv-docling/Scripts/python.exe -m unittest discover -s pipeline_sandbox/tests -v
Remove-Item Env:LAB_TEST_DOCLING
```

No real LLM test is run by unittest. A passing offline suite is not proof that your messy files are correctly understood. Test actual outputs, source coverage, grouping and values yourself before promoting any design into production.

## Cleanup

All code, generated samples, run artifacts and optional environments live under `pipeline_sandbox/`. Generated inputs/runs and environments are ignored by this folder's `.gitignore`, but outputs contain potentially sensitive text; do not publish them. Nothing needs to be migrated into the production application. Remove the folder when you are finished, after retaining any results you actually need.
