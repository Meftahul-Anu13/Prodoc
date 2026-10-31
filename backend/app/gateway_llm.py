"""OpenAI-compatible gateway provider (e.g. the local CLI proxy on port 20133).

Same two-stage observe/extract contract as the other providers. Output is
requested with a JSON-schema response_format and validated again with the
strict Pydantic models; grounding and financial checks downstream are unchanged.
Settings come from gateway_settings.py (git-ignored) or GATEWAY_* variables.
"""
import json
import os
import urllib.error
import urllib.request

from pydantic import ValidationError

from .observation import (EXTRACTION_SYSTEM, OBSERVATION_SYSTEM, Extraction, Observation, ObservationError,
                          StrictModel, _check_context, _decimal_style, _json, _label)


def _settings() -> tuple[str, str, str]:
    try:
        from . import gateway_settings as saved
    except ImportError:
        saved = None
    url = os.getenv("GATEWAY_URL") or getattr(saved, "URL", "")
    token = os.getenv("GATEWAY_TOKEN") or getattr(saved, "TOKEN", "")
    model = os.getenv("GATEWAY_MODEL") or getattr(saved, "MODEL", "")
    return url.rstrip("/"), token, model


class GatewayObservationProvider:
    def __init__(self):
        self.url, self.token, self.model = _settings()
        try:
            self.timeout = float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "280"))
        except ValueError:
            raise ObservationError("gateway_invalid_configuration") from None
        if not self.url.startswith(("http://", "https://")) or not self.token or not _label(self.model):
            raise ObservationError("gateway_invalid_configuration")

    def observe(self, bundle: dict) -> tuple[Observation, dict]:
        return self._request("observation", bundle, Observation, OBSERVATION_SYSTEM)

    def extract(self, bundle: dict, observation: Observation) -> tuple[Extraction, dict]:
        data = {"bundle": bundle, "observation": observation.model_dump(), "money_decimal_style": _decimal_style()}
        return self._request("extraction", data, Extraction, EXTRACTION_SYSTEM)

    def _request(self, stage: str, data: dict, schema: type[StrictModel], system: str):
        content = _json(data)
        _check_context({"system": system, "content": content, "schema": schema.model_json_schema()})
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                # Non-strict: Pydantic's optional/nullable schema is not always
                # accepted by strict mode; strict validation happens locally.
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": stage, "strict": False, "schema": schema.model_json_schema()}}}
        metadata = {"provider": "gateway", "requested_model": self.model}
        response = self._post(body, metadata)
        try:
            choice = response["choices"][0]
            usage = response.get("usage") or {}
            metadata.update(model=response.get("model"), stop_reason=choice.get("finish_reason"),
                            usage={"input_tokens": usage.get("prompt_tokens") or 0, "output_tokens": usage.get("completion_tokens") or 0})
            if choice.get("finish_reason") != "stop":
                raise ObservationError("gateway_incomplete_output", metadata)
            raw = (choice["message"].get("content") or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            return schema.model_validate_json(raw), metadata
        except ObservationError:
            raise
        except (ValidationError, ValueError, TypeError, KeyError, IndexError):
            raise ObservationError("gateway_schema_invalid", metadata) from None

    def _post(self, body: dict, metadata: dict) -> dict:
        request = urllib.request.Request(self.url + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = {401: "gateway_authentication_failed", 403: "gateway_authentication_failed",
                    404: "gateway_model_or_endpoint_missing", 429: "gateway_rate_limited"}.get(exc.code, "gateway_request_failed")
            raise ObservationError(code, metadata) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise ObservationError("gateway_unavailable", metadata) from None
        except (ValueError, TypeError):
            raise ObservationError("gateway_response_invalid", metadata) from None
