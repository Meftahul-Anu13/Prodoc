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
    state = {"replies": [], "requests": [], "status": 200, "statuses": [], "headers": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"].append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            status = state["statuses"].pop(0) if state["statuses"] else state["status"]
            self.send_response(status)
            for name, value in state["headers"].items():
                self.send_header(name, value)
            self.end_headers()
            if status == 200:
                self.wfile.write(json.dumps(state["replies"].pop(0)).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GATEWAY_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("GATEWAY_MODEL", "test-model")
    monkeypatch.setenv("GATEWAY_RETRY_DELAY_SECONDS", "0")
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


def test_temporary_proxy_errors_are_retried_then_succeed(gateway):
    gateway["statuses"] = [408, 503]
    gateway["replies"].append(completion(json.dumps(OBSERVATION)))
    observation, _ = GatewayObservationProvider().observe(BUNDLE)
    assert isinstance(observation, Observation) and len(gateway["requests"]) == 3


def test_retries_are_bounded_and_permanent_errors_are_not_retried(gateway):
    gateway["status"] = 503
    with pytest.raises(ObservationError) as error:
        GatewayObservationProvider().observe(BUNDLE)
    assert error.value.code == "gateway_request_failed" and len(gateway["requests"]) == 3
    gateway["requests"].clear()
    gateway["status"] = 401
    with pytest.raises(ObservationError) as error:
        GatewayObservationProvider().observe(BUNDLE)
    assert error.value.code == "gateway_authentication_failed" and len(gateway["requests"]) == 1


def test_rate_limits_get_more_patience_than_other_errors(gateway):
    gateway["statuses"] = [429, 429, 429, 429]
    gateway["replies"].append(completion(json.dumps(OBSERVATION)))
    observation, _ = GatewayObservationProvider().observe(BUNDLE)
    assert isinstance(observation, Observation) and len(gateway["requests"]) == 5
    gateway["requests"].clear()
    gateway["status"] = 429
    with pytest.raises(ObservationError) as error:
        GatewayObservationProvider().observe(BUNDLE)
    assert error.value.code == "gateway_rate_limited" and len(gateway["requests"]) == 5


def test_a_spent_quota_fails_immediately_instead_of_retrying(gateway):
    gateway["status"] = 429
    gateway["headers"] = {"Retry-After": "10010"}
    with pytest.raises(ObservationError) as error:
        GatewayObservationProvider().observe(BUNDLE)
    assert error.value.code == "gateway_rate_limited" and len(gateway["requests"]) == 1
    gateway["requests"].clear()
    gateway["headers"] = {"Retry-After": "0"}  # a short cooldown is still worth waiting for
    with pytest.raises(ObservationError):
        GatewayObservationProvider().observe(BUNDLE)
    assert len(gateway["requests"]) == 5


def test_a_dead_proxy_fails_after_the_retries(monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("GATEWAY_MODEL", "test-model")
    monkeypatch.setenv("GATEWAY_RETRY_DELAY_SECONDS", "0")
    with pytest.raises(ObservationError) as error:
        GatewayObservationProvider().observe(BUNDLE)
    assert error.value.code == "gateway_unavailable"


def test_gateway_route_uses_gateway_provider(tmp_path, monkeypatch, gateway):
    monkeypatch.setenv("EXTRACTION_PROVIDER", "gateway")
    seen = {}
    monkeypatch.setattr("backend.app.sources.read_sources", lambda path, name, out: {**BUNDLE, "metadata": {"reader": "test"}})
    monkeypatch.setattr("backend.app.observation.observe_extract",
                        lambda bundle, artifact_dir, provider=None: seen.setdefault("p", type(provider).__name__) and {"invoices": [], "metadata": {}})
    result = parse_job.process_document(tmp_path / "x.csv", "x.csv", tmp_path / "run")
    assert seen["p"] == "GatewayObservationProvider" and result["metadata"]["requested_provider"] == "gateway"
