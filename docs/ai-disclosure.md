# AI disclosure and licensing

## AI-assisted development

This project was built with Claude Code (Anthropic), mostly on Claude Opus 5.5, with Claude Sonnet subagents for some research and review. AI wrote most of the source code, tests, fictional fixtures and documentation under the developer's direction.

The developer set the requirements and direction, chose the design (for example, replacing Azure with MinerU and adding the guardrail), tested the running application, and reported the defects that were fixed. AI also ran the local verification.

The prompts are in [prompts/PROMPTS.md](../prompts/PROMPTS.md). Development prompts there are condensed.

## AI at runtime

| Purpose | Default | Runs where |
|---|---|---|
| Layout observation and extraction | `gpt-6.1-sol` through an OpenAI-compatible gateway | Wherever the gateway points |
| Same, free option | Ollama with `qwen3:4b-instruct-2507-q4_K_M` | This machine |
| Same, optional | Claude through the Anthropic SDK, needs `ALLOW_CLOUD_EXTRACTION=true` | Anthropic |
| OCR and layout | MinerU 4.0.12 Basic/ONNX | This machine |
| Search embeddings | BAAI/bge-small-en-v1.5 | This machine |

No model was trained or fine-tuned. Prompts and schemas live in `backend/app/observation.py`, versioned as `observation-extraction-v1`.

## What is sent to the model

Both stages receive recovered text, cell values, sheet names and layout metadata. They don't receive the original PDF or image file, but the text can contain everything sensitive in the document, so choose the gateway accordingly. Ollama keeps everything on the machine.

Document content is treated as untrusted. The model has no tools and takes no actions. Its output must pass strict schemas and source grounding, and a person finalizes every invoice.

## Evaluation disclosure

Evaluation documents are generated, fictional and seed-separated from development data. Labels come from the generator, not from independent human review, and template variety is limited. Results don't predict real-world accuracy. No prompt or threshold was tuned on the test split.

## Third-party materials

This is an inventory, not legal advice. Check exact licenses before redistribution or commercial use. The project license hasn't been chosen.

| Component | Purpose | Note |
|---|---|---|
| MinerU, DocVortex and their models | OCR and layout | Model and transitive licenses need review |
| PyMuPDF / MuPDF | PDF text and previews | **AGPL or commercial dual license** |
| FastAPI, SQLAlchemy, React, Vite | App framework | Permissive |
| FastEmbed, BGE-small, Qdrant client | Semantic search | Model and package licenses are separate |
| RapidOCR, ONNX Runtime | Baseline comparison only | Engine and models are licensed separately |
| Pillow, openpyxl, xlrd, xlwt | Images and spreadsheets | Check pinned versions |
| Anthropic SDK | Optional provider | Hosted service terms apply |
| Ollama and Qwen weights | Optional local LLM | Model license applies |
| Playwright, Chrome | Browser checks | Not part of the running app |
