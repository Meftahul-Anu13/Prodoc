"""Local Ollama provider against an in-process fake server; no model or network needed."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.app import parse_job
from backend.app.local_llm import OllamaObservationProvider
from backend.app.observation import Observation, ObservationError

BUNDLE = {"blocks": [{"id": "b1", "text": "Invoice INV-1", "location": {"page": 1}},
                     {"id": "b2", "text": "Total 110.00", "location": {"page": 1}}],
          "pages": [{"page": 1, "text": "duplicated page text"}], "warnings": [], "metadata": {}}
OBSERVATION = {"layout": "single invoice", "regions": [{"id": "r1", "source_ids": ["b1", "b2"], "purpose": "header", "layout": "lines"}],
               "groups": [{"id": "g1", "region_ids": ["r1"], "notes": ""}], "ignored_source_ids": [], "warnings": []}


@pytest.fixture
def ollama(monkeypatch):
    state = {"replies": [], "requests": [], "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if state["status"] != 200:
                self.send_response(state["status"])
                self.end_headers()
                return
            reply = state["replies"].pop(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    yield state
    server.shutdown()


def reply(content, reason="stop", prompt_tokens=100):
    return {"model": "qwen3:4b", "message": {"content": json.dumps(content) if not isinstance(content, str) else content},
            "done_reason": reason, "prompt_eval_count": prompt_tokens, "eval_count": 50}


def test_observe_sends_schema_constraint_temperature_zero_and_compact_blocks(ollama):
    ollama["replies"].append(reply(OBSERVATION))
    observation, metadata = OllamaObservationProvider().observe(BUNDLE)
    assert isinstance(observation, Observation) and observation.groups[0].id == "g1"
    request = ollama["requests"][0]
    assert request["format"] == Observation.model_json_schema()
    assert request["options"]["temperature"] == 0 and request["stream"] is False
    user = json.loads(request["messages"][1]["content"])
    assert [b["id"] for b in user["blocks"]] == ["b1", "b2"] and "pages" not in user
    assert metadata["provider"] == "ollama" and metadata["usage"] == {"input_tokens": 100, "output_tokens": 50}


def test_schema_invalid_output_retries_once_then_fails_closed(ollama):
    ollama["replies"] += [reply("{not json"), reply({"layout": "x"})]
    with pytest.raises(ObservationError) as error:
        OllamaObservationProvider().observe(BUNDLE)
    assert error.value.code == "local_llm_schema_invalid" and len(ollama["requests"]) == 2


def test_retry_recovers_from_truncated_output(ollama):
    ollama["replies"] += [reply('{"layout":', reason="length"), reply(OBSERVATION)]
    observation, _ = OllamaObservationProvider().observe(BUNDLE)
    assert observation.regions[0].id == "r1"


def test_prompt_filling_context_is_rejected_not_silently_truncated(ollama, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_NUM_CTX", "8192")
    ollama["replies"].append(reply(OBSERVATION, prompt_tokens=8000))
    with pytest.raises(ObservationError) as error:
        OllamaObservationProvider().observe(BUNDLE)
    assert error.value.code == "local_llm_context_exceeded"


def test_oversized_input_fails_before_any_request(ollama):
    big = {**BUNDLE, "blocks": [{"id": f"b{i}", "text": "x" * 200, "location": {}} for i in range(2000)]}
    with pytest.raises(ObservationError) as error:
        OllamaObservationProvider().observe(big)
    assert error.value.code == "local_llm_context_exceeded" and ollama["requests"] == []


@pytest.mark.parametrize("url", ["http://example.com:11434", "https://127.0.0.1:11434", "http://10.0.0.5:11434"])
def test_only_plain_loopback_endpoints_are_allowed(monkeypatch, url):
    monkeypatch.setenv("OLLAMA_URL", url)
    with pytest.raises(ObservationError) as error:
        OllamaObservationProvider()
    assert error.value.code == "local_llm_invalid_configuration"


def test_missing_model_and_unreachable_server_have_clear_codes(ollama, monkeypatch):
    ollama["status"] = 404
    with pytest.raises(ObservationError) as error:
        OllamaObservationProvider().observe(BUNDLE)
    assert error.value.code == "local_llm_model_missing"
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    with pytest.raises(ObservationError) as error:
        OllamaObservationProvider().observe(BUNDLE)
    assert error.value.code == "local_llm_unavailable"


def test_local_route_needs_no_cloud_consent_and_uses_local_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACTION_PROVIDER", "local")
    monkeypatch.setenv("ALLOW_CLOUD_EXTRACTION", "false")
    seen = {}
    monkeypatch.setattr("backend.app.sources.read_sources", lambda path, name, out: {**BUNDLE, "metadata": {"reader": "test"}})
    def fake(bundle, artifact_dir, provider=None):
        seen["provider"] = type(provider).__name__
        return {"invoices": [], "metadata": {}}
    monkeypatch.setattr("backend.app.observation.observe_extract", fake)
    result = parse_job.process_document(tmp_path / "x.csv", "x.csv", tmp_path / "run")
    assert seen["provider"] == "OllamaObservationProvider"
    assert result["metadata"]["requested_provider"] == "local"


def test_no_groups_gets_labeled_single_invoice_fallback(ollama):
    ollama["replies"].append(reply({**OBSERVATION, "groups": []}))
    observation, _ = OllamaObservationProvider().observe(BUNDLE)
    assert [(g.id, g.region_ids) for g in observation.groups] == [("g_auto", ["r1"])]
    assert any("auto_group_fallback" in w for w in observation.warnings)
    assert ollama["requests"][0]["keep_alive"] == "30m"


def test_unknown_group_id_is_remapped_only_when_one_group_exists(ollama):
    invoice = {"group_id": "g9", "line_items": []}
    from backend.app.observation import Extraction, InvoiceCandidate
    fields = {k: {"value": None, "source_id": None, "region_id": None, "quote": None}
              for k in InvoiceCandidate.model_fields if k not in {"group_id", "line_items"}}
    ollama["replies"].append(reply({"invoices": [{**fields, **invoice}], "warnings": []}))
    extraction, _ = OllamaObservationProvider().extract(BUNDLE, Observation.model_validate(OBSERVATION))
    assert extraction.invoices[0].group_id == "g1"
    assert any("group_id_remapped" in w for w in extraction.warnings)
    two = {**OBSERVATION, "groups": [*OBSERVATION["groups"], {"id": "g2", "region_ids": ["r1"], "notes": ""}]}
    ollama["replies"].append(reply({"invoices": [{**fields, **invoice}], "warnings": []}))
    extraction, _ = OllamaObservationProvider().extract(BUNDLE, Observation.model_validate(two))
    assert extraction.invoices[0].group_id == "g9"
