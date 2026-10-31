"""OpenAI-compatible gateway provider against an in-process fake server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.app import parse_job
from backend.app.gateway_llm import GatewayObservationProvider
from backend.app.observation import Observation, ObservationError

BUNDLE = {"blocks": [{"id": "b1", "text": "Invoice INV-1", "location": {"page": 1}}], "pages": [], "warnings": [], "metadata": {}}
OBSERVATION = {"layout": "one", "regions": [{"id": "r1", "source_ids": ["b1"], "purpose": "header", "layout": "line"}],
               "groups": [{"id": "g1", "region_ids": ["r1"], "notes": ""}], "ignored_source_ids": [], "warnings": []}


@pytest.fixture
def gateway(monkeypatch):
    state = {"replies": [], "requests": [], "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"].append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(state["status"])
            self.end_headers()
            if state["status"] == 200:
                self.wfile.write(json.dumps(state["replies"].pop(0)).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GATEWAY_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("GATEWAY_MODEL", "test-model")
    yield state
    server.shutdown()


def completion(content, finish="stop"):
    return {"model": "test-model", "choices": [{"finish_reason": finish, "message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_observe_sends_bearer_schema_and_validates(gateway):
    gateway["replies"].append(completion("```json\n" + json.dumps(OBSERVATION) + "\n```"))
    observation, metadata = GatewayObservationProvider().observe(BUNDLE)
    assert isinstance(observation, Observation) and metadata["usage"] == {"input_tokens": 10, "output_tokens": 5}
    path, auth, body = gateway["requests"][0]
    assert path == "/v1/chat/completions" and auth == "Bearer test-token"
    assert body["model"] == "test-model" and body["temperature"] == 0
    assert body["response_format"]["json_schema"]["schema"] == Observation.model_json_schema()


@pytest.mark.parametrize("reply,code", [(completion("{}"), "gateway_schema_invalid"),
                                        (completion(json.dumps(OBSERVATION), "length"), "gateway_incomplete_output")])
def test_invalid_or_truncated_output_fails_closed(gateway, reply, code):
    gateway["replies"].append(reply)
    with pytest.raises(ObservationError) as error:
        GatewayObservationProvider().observe(BUNDLE)
    assert error.value.code == code


def test_auth_failure_and_missing_settings_have_clear_codes(gateway, monkeypatch):
    gateway["status"] = 401
    with pytest.raises(ObservationError) as error:
        GatewayObservationProvider().observe(BUNDLE)
    assert error.value.code == "gateway_authentication_failed"
    monkeypatch.setenv("GATEWAY_URL", "not-a-url")
    with pytest.raises(ObservationError) as error:
        GatewayObservationProvider()
    assert error.value.code == "gateway_invalid_configuration"


def test_gateway_route_uses_gateway_provider(tmp_path, monkeypatch, gateway):
    monkeypatch.setenv("EXTRACTION_PROVIDER", "gateway")
    seen = {}
    monkeypatch.setattr("backend.app.sources.read_sources", lambda path, name, out: {**BUNDLE, "metadata": {"reader": "test"}})
    monkeypatch.setattr("backend.app.observation.observe_extract",
                        lambda bundle, artifact_dir, provider=None: seen.setdefault("p", type(provider).__name__) and {"invoices": [], "metadata": {}})
    result = parse_job.process_document(tmp_path / "x.csv", "x.csv", tmp_path / "run")
    assert seen["p"] == "GatewayObservationProvider" and result["metadata"]["requested_provider"] == "gateway"
