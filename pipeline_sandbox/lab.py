"""Standalone manual experiment; never imported by the production application."""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .readers import LabError, json_write, read_source
from .reasoning import (EXTRACT_PROMPT, OBSERVE_PROMPT, Extraction, Observation,
                        context_for, ground_and_validate, validate_observation)

ROOT = Path(__file__).resolve().parent


def _parse_child(path: str, output: str, settings: dict) -> None:
    try:
        read_source(Path(path), Path(output), **settings)
    except Exception as exc:
        # Local parser diagnostics only; do not expose arbitrary document content in logs.
        message = str(exc) if isinstance(exc, LabError) else type(exc).__name__
        json_write(Path(output) / "parser_error.json", {"error": message, "type": type(exc).__name__})
        raise SystemExit(2)


def prepare(path: Path, output: Path, settings: dict, timeout: int) -> dict:
    if timeout < 1 or timeout > 1800:
        raise LabError("Parser timeout must be between 1 and 1800 seconds.")
    # Process timeout is not an OS security sandbox; see README for parser risks.
    child = multiprocessing.get_context("spawn").Process(
        target=_parse_child, args=(str(path.resolve()), str(output), settings))
    started = time.perf_counter()
    child.start()
    child.join(timeout)
    if child.is_alive():
        child.terminate()
        child.join(5)
        if child.is_alive():
            child.kill()
            child.join()
        raise LabError("Parser timed out; no LLM call was made. Inspect partial local artifacts.")
    if child.exitcode != 0:
        error_file = output / "parser_error.json"
        detail = json.loads(error_file.read_text(encoding="utf-8"))["error"] if error_file.exists() else f"exit {child.exitcode}"
        raise LabError(f"Local source reading failed: {detail}")
    bundle = json.loads((output / "source.json").read_text(encoding="utf-8"))
    bundle["metadata"]["parse_seconds"] = round(time.perf_counter() - started, 4)
    json_write(output / "source.json", bundle)
    return bundle


def call_model(stage: str, schema: type, system: str, payload: str, output: Path,
               model: str, max_tokens: int) -> dict:
    import anthropic

    # The SDK resolves explicitly configured credentials. No production .env import.
    # No tools, SDK retries, autonomous loop, or program execution by the model.
    started = time.perf_counter()
    try:
        with anthropic.Anthropic(timeout=180, max_retries=0) as client:
            with client.beta.messages.stream(
                model=model, max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema.model_json_schema()}},
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},
                system=system,
                messages=[{"role": "user", "content": "Interpret only the following JSON source DATA:\n" + payload}],
            ) as stream:
                message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise LabError("LLM authentication failed. Configure credentials in your shell; never paste them into source files.") from exc
    except anthropic.PermissionDeniedError as exc:
        raise LabError("LLM provider denied access; no automatic provider switch.") from exc
    except anthropic.NotFoundError as exc:
        raise LabError("Model/endpoint not found; verify the model configured for your account.") from exc
    except anthropic.RateLimitError as exc:
        raise LabError("LLM rate limit; no automatic retry or duplicate billed call.") from exc
    except anthropic.APIStatusError as exc:
        raise LabError(f"LLM HTTP error {exc.status_code}; no automatic retry. Check SDK/model compatibility.") from exc
    except anthropic.APIConnectionError as exc:
        raise LabError("LLM connection/timeout failure. An interrupted request may still be billed; no automatic retry.") from exc
    metadata = {"stage": stage, "requested_model": model, "served_model": message.model,
                "stop_reason": message.stop_reason, "usage": message.usage.model_dump(mode="json"),
                "seconds": round(time.perf_counter() - started, 4),
                "request_id": getattr(message, "_request_id", None), "fallbacks_enabled": True}
    json_write(output / f"{stage}_usage.json", metadata)
    text = "\n".join(block.text for block in message.content if block.type == "text")
    (output / f"{stage}_response.txt").write_text(text, encoding="utf-8")
    if message.stop_reason != "end_turn":
        raise LabError(f"{stage}: stop_reason={message.stop_reason}; output was not accepted as complete.")
    parsed = schema.model_validate_json(text)
    json_write(output / f"{stage}.json", parsed.model_dump(mode="json"))
    return parsed


def run_reasoning(bundle: dict, output: Path, model: str, max_chars: int, max_tokens: int,
                  locale: str, tolerance: Decimal, observe_only: bool = False, caller=call_model) -> dict:
    source = context_for(bundle, max_chars)
    # Fixed two-stage experiment: observe layout first, then extract using source evidence.
    observation = caller("observation", Observation, OBSERVE_PROMPT, source, output, model, max_tokens)
    coverage = validate_observation(observation, bundle["blocks"])
    json_write(output / "observation_coverage.json", coverage)
    if observe_only:
        return {"status": "observation_only", "coverage": coverage}
    extraction_context = json.dumps({"source": json.loads(source), "proposed_observation": observation.model_dump()}, ensure_ascii=False)
    if len(extraction_context) > max_chars:
        raise LabError("Source plus observation exceeds --max-chars; observation is saved. Extraction was NOT called and no context was truncated.")
    extraction = caller("candidates", Extraction, EXTRACT_PROMPT, extraction_context, output, model, max_tokens)
    result = ground_and_validate(extraction, observation, bundle, locale, tolerance)
    json_write(output / "checked_results.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Disposable pipeline lab: no fixed-header assumptions, no production imports.")
    parser.add_argument("action", choices=["prepare", "observe", "run"])
    parser.add_argument("input", type=Path)
    parser.add_argument("--parser", choices=["auto", "docling", "mineru"], default="auto")
    parser.add_argument("--mineru-tier", help="Explicit profile for your installed MinerU release; no backend auto-selection guarantee.")
    parser.add_argument("--artifacts-path", help="Prefetched local Docling model directory.")
    parser.add_argument("--encoding")
    parser.add_argument("--delimiter", help="One character, e.g. ';'. Native readers never assume headers.")
    parser.add_argument("--allow-cloud", action="store_true", help="Consent to send source text/cells to the configured Anthropic endpoint and incur API charges.")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--max-chars", type=int, default=80_000)
    parser.add_argument("--max-output-tokens", type=int, default=16_000)
    parser.add_argument("--parse-timeout", type=int, default=180)
    parser.add_argument("--decimal-style", choices=["dot", "comma"], default="dot")
    parser.add_argument("--tolerance", default="0.01", help="Explicit test comparison policy, not automatic ISO currency policy.")
    args = parser.parse_args(argv)
    output: Path | None = None
    try:
        if args.action != "prepare" and not args.allow_cloud:
            raise LabError("No document sent. observe/run require --allow-cloud (paid external API). Use prepare for local inspection only.")
        if not 1000 <= args.max_chars <= 200_000 or not 1024 <= args.max_output_tokens <= 32_000:
            raise LabError("Bounds: --max-chars 1000..200000; --max-output-tokens 1024..32000.")
        tolerance = Decimal(args.tolerance)
        if not tolerance.is_finite() or not Decimal(0) <= tolerance <= Decimal(1):
            raise LabError("--tolerance must be finite, between 0 and 1.")
        if not args.input.is_file():
            raise LabError("Input file does not exist.")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        output = ROOT / "runs" / run_id
        output.mkdir(parents=True, exist_ok=False)
        json_write(output / "run.json", {"schema": "disposable-run-v1", "action": args.action,
                   "input": args.input.name, "cloud_authorized": args.allow_cloud,
                   "model": args.model if args.action != "prepare" else None,
                   "status": "started", "synthetic_test_replay": False})
        print(f"Local artifacts: {output}", flush=True)
        settings = {"parser": args.parser, "encoding": args.encoding, "delimiter": args.delimiter,
                    "mineru_tier": args.mineru_tier, "artifacts_path": args.artifacts_path}
        bundle = prepare(args.input, output, settings, args.parse_timeout)
        print(f"Read {len(bundle['blocks'])} nonempty source blocks. No header mapping applied.", flush=True)
        if args.action == "prepare":
            context_for(bundle, args.max_chars)
            print("PREPARED ONLY: inspect source.json. No LLM called; no invoice extraction claim.")
            outcome = {"status": "prepared_only"}
        else:
            print("External text/cell transmission authorized. Server refusal fallbacks enabled; they can add billed attempts. No SDK retries.", flush=True)
            print("Calling layout observation" + (" only." if args.action == "observe" else ", then evidence-based extraction (two logical requests)."), flush=True)
            outcome = run_reasoning(bundle, output, args.model, args.max_chars, args.max_output_tokens,
                                    args.decimal_style, tolerance, observe_only=args.action == "observe")
            print(f"Outcome: {outcome['status']}. All extracted records require human review.")
        json_write(output / "outcome.json", {"status": outcome["status"], "real_llm_called": args.action != "prepare"})
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, LabError) else f"{type(exc).__name__}; inspect configuration/input. No successful result is claimed."
        if output:
            json_write(output / "error.json", {"type": type(exc).__name__, "message": message, "status": "failed"})
        print(f"ERROR: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
