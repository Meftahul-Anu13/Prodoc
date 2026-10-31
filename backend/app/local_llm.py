"""Local open-weight provider via Ollama: free, offline, no cloud consent needed.

Implements the same two-stage observe/extract contract as the Anthropic
provider. Output is constrained by Ollama's JSON-schema `format`, then validated
again with the strict Pydantic models; grounding and financial checks downstream
are unchanged. Only loopback endpoints are accepted so documents never leave the
machine. Ollama silently truncates prompts longer than `num_ctx`, so oversized
inputs fail closed before and after the request instead of losing evidence.
"""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

from pydantic import ValidationError

from .observation import (EXTRACTION_SYSTEM, OBSERVATION_SYSTEM, Extraction, Observation, ObservationError,
                          StrictModel, _decimal_style, _json, _label)

DEFAULT_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_NUM_CTX = 8192  # 16k context needs ~1.2 GB more RAM, which an 8 GB laptop cannot spare.
OUTPUT_RESERVE = 3072
CHARS_PER_TOKEN = 3  # Conservative pre-check; actual prompt tokens are checked after the call.
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
KEEP_ALIVE = "30m"  # Avoid reloading the model from disk for every stage/document.
AUTO_GROUP = "g_auto"

# Small models need the region/group contract spelled out; the cloud prompts stay unchanged.
LOCAL_OBSERVATION_HINT = """
Small-model rules: every region that holds invoice evidence MUST belong to a
group. If the data is one invoice, return exactly one group listing all of its
region ids, for example: regions [{"id":"r1","source_ids":["b1","b2"],...}],
groups [{"id":"g1","region_ids":["r1"],"notes":"single invoice"}].
"""
LOCAL_EXTRACTION_HINT = """
Small-model rules: group_id MUST be one of the group ids in the observation, and
region_id MUST be a region of that group. quote is copied character-for-character
from the cited block. Numeric value strings contain only the number, e.g. "110.00".
"""


def _compact(bundle: dict) -> dict:
    # Blocks carry every piece of evidence; page text duplicates them and would
    # only spend scarce local context.
    return {"blocks": [{"id": b["id"], "text": b["text"], "location": b.get("location", {})} for b in bundle["blocks"]],
            "warnings": bundle.get("warnings", [])}


class OllamaObservationProvider:
    def __init__(self):
        self.url = os.getenv("OLLAMA_URL", DEFAULT_URL).rstrip("/")
        self.model = os.getenv("LOCAL_LLM_MODEL", DEFAULT_MODEL)
        try:
            self.num_ctx = int(os.getenv("LOCAL_LLM_NUM_CTX", str(DEFAULT_NUM_CTX)))
            self.timeout = float(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "900"))
        except ValueError:
            raise ObservationError("local_llm_invalid_configuration") from None
        parsed = urlparse(self.url)
        if parsed.scheme != "http" or parsed.hostname not in LOOPBACK or not _label(self.model) or self.num_ctx <= OUTPUT_RESERVE:
            raise ObservationError("local_llm_invalid_configuration")

    def observe(self, bundle: dict) -> tuple[Observation, dict]:
        observation, metadata = self._request(_compact(bundle), Observation, OBSERVATION_SYSTEM + LOCAL_OBSERVATION_HINT)
        if not observation.groups and observation.regions:
            # Labeled assumption, not a hidden repair: treat the document as one
            # invoice. Grounding still checks every value; review is still required.
            observation = observation.model_copy(update={
                "groups": [{"id": AUTO_GROUP, "region_ids": [r.id for r in observation.regions],
                            "notes": "automatic single-invoice fallback: the local model returned no groups"}],
                "warnings": [*observation.warnings, "local_llm_auto_group_fallback: verify this is one invoice"]})
            observation = Observation.model_validate(observation.model_dump())
        return observation, metadata

    def extract(self, bundle: dict, observation: Observation) -> tuple[Extraction, dict]:
        data = {"bundle": _compact(bundle), "observation": observation.model_dump(), "money_decimal_style": _decimal_style()}
        extraction, metadata = self._request(data, Extraction, EXTRACTION_SYSTEM + LOCAL_EXTRACTION_HINT)
        known = {group.id for group in observation.groups}
        if len(known) == 1:
            # With exactly one group the intended group is unambiguous; a
            # mistyped id would otherwise discard every grounded value.
            (only,) = known
            fixed = [invoice.model_copy(update={"group_id": only}) for invoice in extraction.invoices]
            if any(invoice.group_id not in known for invoice in extraction.invoices):
                extraction = extraction.model_copy(update={"invoices": fixed, "warnings": [
                    *extraction.warnings, "local_llm_group_id_remapped: model cited an unknown group in a single-group document"]})
        return extraction, metadata

    def _request(self, data: dict, schema: type[StrictModel], system: str):
        schema_json = schema.model_json_schema()
        # Small models follow a schema better when it is also stated in the prompt.
        prompt = system + "\nJSON schema:\n" + _json(schema_json)
        content = _json(data)
        if (len(prompt) + len(content)) / CHARS_PER_TOKEN > self.num_ctx - OUTPUT_RESERVE:
            raise ObservationError("local_llm_context_exceeded")
        body = {"model": self.model, "stream": False, "format": schema_json, "keep_alive": KEEP_ALIVE,
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
                "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": OUTPUT_RESERVE}}
        metadata = {"provider": "ollama", "requested_model": self.model}
        last = "local_llm_schema_invalid"
        # One retry: a local call costs nothing, and constrained decoding can
        # still stop mid-object at num_predict.
        for _ in range(2):
            response = self._post(body, metadata)
            metadata.update(model=response.get("model"), stop_reason=response.get("done_reason"),
                            usage={"input_tokens": response.get("prompt_eval_count") or 0,
                                   "output_tokens": response.get("eval_count") or 0})
            if metadata["usage"]["input_tokens"] >= self.num_ctx - OUTPUT_RESERVE:
                raise ObservationError("local_llm_context_exceeded", metadata)
            if response.get("done_reason") != "stop":
                last = "local_llm_incomplete_output"
                continue
            try:
                return schema.model_validate_json(response["message"]["content"]), metadata
            except (ValidationError, ValueError, TypeError, KeyError):
                last = "local_llm_schema_invalid"
        raise ObservationError(last, metadata)

    def _post(self, body: dict, metadata: dict) -> dict:
        request = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Ollama's error text describes server state (memory, model), never
            # document content, but only a stable code leaves this process.
            try:
                detail = exc.read().decode("utf-8", "replace").lower()
            except Exception:
                detail = ""
            if exc.code == 404:
                code = "local_llm_model_missing"
            elif "memory" in detail:
                code = "local_llm_insufficient_memory"
            else:
                code = "local_llm_request_failed"
            raise ObservationError(code, metadata) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise ObservationError("local_llm_unavailable", metadata) from None
        except (ValueError, TypeError):
            raise ObservationError("local_llm_response_invalid", metadata) from None
