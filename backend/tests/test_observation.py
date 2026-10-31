"""Offline replays only: no test is authorized to contact a paid/live provider."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend.app.observation import (
    AnthropicObservationProvider, Extraction, GroundedField, InvoiceCandidate,
    InvoiceGroup, LineCandidate, Observation, ObservationError, Region,
    observe_extract, parse_decimal,
)


INVOICE_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name", "currency", "subtotal", "tax", "total", "discount", "other_charges", "line_amount_basis")


@pytest.fixture(autouse=True)
def deny_live_sdk(monkeypatch):
    import anthropic
    # Every test must explicitly replace this mock to exercise the SDK adapter.
    monkeypatch.setattr(anthropic, "Anthropic", Mock(side_effect=AssertionError("No live calls authorized")))
    for key in ("ALLOW_CLOUD_EXTRACTION", "OBSERVATION_MAX_CONTEXT_BYTES", "MONEY_DECIMAL_STYLE", "LLM_MODEL", "OBSERVATION_MODEL", "EXTRACTION_MODEL", "OBSERVATION_EFFORT", "EXTRACTION_EFFORT"):
        monkeypatch.delenv(key, raising=False)


def bundle_of(*texts):
    return {"blocks": [{"id": f"s{i}", "text": text, "location": {"page": i + 1, "sheet": "Transposed", "cell": f"B{i + 2}", "method": "spreadsheet"}} for i, text in enumerate(texts)], "pages": [], "warnings": [], "metadata": {"format": "xlsx"}}


def observe_all(bundle):
    return Observation(layout="Transposed mixed-layout invoices, not invoice-per-row", regions=[Region(id="r", source_ids=[block["id"] for block in bundle["blocks"]], purpose="invoices", layout="columns and multiline items")], groups=[InvoiceGroup(id="g", region_ids=["r"], notes="Multiple invoices in one table")], ignored_source_ids=[], warnings=[])


def field(value=None, source_id="s0", region_id="r", quote=None):
    return GroundedField(value=value, source_id=source_id if value is not None else None, region_id=region_id if value is not None else None, quote=quote if quote is not None else value)


def candidate(**values):
    return InvoiceCandidate(group_id="g", **{name: values.get(name, field()) for name in INVOICE_FIELDS}, line_items=values.get("line_items", []))


class Replay:
    def __init__(self, observation, extraction):
        self.observation, self.extraction = observation, extraction
        self.calls = []

    def observe(self, bundle):
        self.calls.append(("observation", copy.deepcopy(bundle)))
        return self.observation, {"provider": "offline-replay", "model": "layout-replay", "requested_model": "layout-replay", "usage": {"input_tokens": 30, "output_tokens": 10}, "api_key": "never-save-this"}

    def extract(self, bundle, observation):
        self.calls.append(("extraction", copy.deepcopy(bundle), observation))
        return self.extraction, {"provider": "offline-replay", "model": "extract-replay", "requested_model": "extract-replay", "usage": {"input_tokens": 40, "output_tokens": 20}, "error": "never-save-this"}


def replay_for(bundle, *invoices, observation=None):
    return Replay(observation or observe_all(bundle), Extraction(invoices=list(invoices), warnings=[]))


def test_transposed_multiline_multi_invoice_uses_two_stages_and_original_blocks(tmp_path):
    bundle = bundle_of("Invoice INV-A | INV-B", "Vendor Acme", "Widget 2 10.00 20.00", "Service 1 5.00 5.00", "Total 25.00 | 30.00")
    def line(description, quantity, unit_price, total, source_id):
        quote = bundle["blocks"][int(source_id[1:])]["text"]
        return LineCandidate(discount=field(), tax=field(), **{name: field(value, source_id=source_id, quote=quote) for name, value in {"description": description, "quantity": quantity, "unit_price": unit_price, "line_total": total}.items()})
    first = candidate(invoice_number=field("INV-A", quote="Invoice INV-A | INV-B"), supplier_name=field("Acme", "s1", quote="Vendor Acme"), total=field("25.00", "s4", quote="Total 25.00 | 30.00"), line_items=[line("Widget", "2", "10.00", "20.00", "s2"), line("Service", "1", "5.00", "5.00", "s3")])
    second = candidate(invoice_number=field("INV-B", quote="Invoice INV-A | INV-B"), total=field("30.00", "s4", quote="Total 25.00 | 30.00"))
    replay = replay_for(bundle, first, second)
    result = observe_extract(bundle, tmp_path, replay)
    assert [call[0] for call in replay.calls] == ["observation", "extraction"]
    assert replay.calls[0][1] == replay.calls[1][1] == bundle
    assert replay.calls[1][2] == replay.observation
    assert [invoice["invoice_number"] for invoice in result["invoices"]] == ["INV-A", "INV-B"]
    assert len(result["invoices"][0]["line_items"]) == 2
    assert all(invoice["needs_review"] for invoice in result["invoices"])
    evidence = result["invoices"][0]["line_items"][1]["evidence"]["line_total"]
    assert evidence["source_id"] == "s3" and evidence["page"] == 4
    assert evidence["sheet"] == "Transposed" and evidence["cell"] == "B5"
    assert evidence["source_text"] == "Service 1 5.00 5.00" and evidence["grounded"] is True
    assert result["metadata"]["coverage"]["all_blocks_accounted_for"] is True
    assert result["metadata"]["coverage"]["completeness_verified"] is False
    assert result["metadata"]["model"] == "extract-replay"
    assert result["metadata"]["requested_model"] == "extract-replay"
    assert result["metadata"]["finalization_blocked"] is False
    assert result["metadata"]["extraction_runs"][0]["usage"]["input_tokens"] == 30
    assert all('/' not in name and '\\' not in name for name in result['metadata']['artifacts'].values())
    for name in ("observation_bundle.json", "observation.json", "extraction.json", "observation_metadata.json", "grounded_invoices.json"):
        saved = (tmp_path / name).read_text(encoding="utf-8")
        json.loads(saved)
        assert "never-save-this" not in saved


@pytest.mark.parametrize("format_name", ["pdf", "png", "csv", "xls", "xlsx"])
def test_every_format_runs_observation_before_extraction(tmp_path, format_name):
    bundle = bundle_of("Invoice INV-9")
    bundle["metadata"]["format"] = format_name
    replay = replay_for(bundle, candidate(invoice_number=field("INV-9", quote="Invoice INV-9")))
    observe_extract(bundle, tmp_path, replay)
    assert [call[0] for call in replay.calls] == ["observation", "extraction"]


@pytest.mark.parametrize("original,quote,value", [
    ("Total 110.00", "110.00", "10.00"),
    ("Total 110.00", "10.00", "10.00"),
    ("Total -10.00", "10.00", "10.00"),
    ("Total -10.00", "-10.00", "10.00"),
    ("Total (10.00)", "10.00", "10.00"),
    ("Total (10.00)", "(10.00)", "10.00"),
    ("Total −10.00", "10.00", "10.00"),
    ("Total – 10.00", "10.00", "10.00"),
    ("Total －10.00", "10.00", "10.00"),
    ("Total 10.00-", "10.00", "10.00"),
    ("Total -0.00", "-0.00", "0.00"),
    ("Total 10.00", "invented 10.00", "10.00"),
    ("Tax rate 10%", "10", "10"),
    ("Total 10.50", "10", "10"),
    ("Total 1,23.00", "1,23.00", "23.00"),
])
def test_numeric_substrings_sign_loss_and_fabricated_quotes_rejected(tmp_path, original, quote, value):
    bundle = bundle_of(original)
    result = observe_extract(bundle, tmp_path, replay_for(bundle, candidate(total=field(value, quote=quote))))
    invoice = result["invoices"][0]
    assert invoice["total"] is None
    assert invoice["evidence"]["total"]["grounded"] is False
    assert any(finding["rule_id"] == "ungrounded_field" for finding in invoice["findings"])


@pytest.mark.parametrize("original,expected", [("Total -10.00", "-10.00"), ("Total (10.00)", "-10.00"), ("Total USD 1,234.50", "1234.50"), ("Total −10.00", "-10.00"), ("Total $-10.00", "-10.00")])
def test_complete_numeric_tokens_preserve_signed_value(tmp_path, original, expected):
    bundle = bundle_of(original)
    value = original.removeprefix("Total ")
    invoice = observe_extract(bundle, tmp_path, replay_for(bundle, candidate(total=field(value, quote=original))))["invoices"][0]
    assert invoice["total"] == expected
    assert invoice["evidence"]["total"]["grounded"] is True


@pytest.mark.parametrize("style,raw,expected", [("dot", "1,234.50", "1234.50"), ("comma", "1.234,50", "1234.50"), ("dot", "1.234", "1.234"), ("comma", "1.234", "1234.00"), ("comma", "1,234", "1.234"), ("dot", "1,234", "1234.00")])
def test_decimal_locale_is_explicit(tmp_path, monkeypatch, style, raw, expected):
    monkeypatch.setenv("MONEY_DECIMAL_STYLE", style)
    bundle = bundle_of("Total " + raw)
    invoice = observe_extract(bundle, tmp_path, replay_for(bundle, candidate(total=field(raw, quote="Total " + raw))))["invoices"][0]
    assert invoice["total"] == expected


@pytest.mark.parametrize("value", ["1,23", "1.234,50", "1e3", "NaN", "Infinity", "--10", "(-10)", "1 23", "10USD20", "1.0000001", "1000000000000000001"])
def test_decimal_parser_rejects_invalid_dot_values(value):
    with pytest.raises(ValueError):
        parse_decimal(value, "dot")


def test_no_inferred_tax_discount_or_total_and_existing_financial_validation(tmp_path):
    bundle = bundle_of("Currency USD Subtotal 20.00 Tax 2.00 Total 99.00 Discount 0.00 Charges 0.00")
    quote = bundle["blocks"][0]["text"]
    first = candidate(currency=field("USD", quote=quote), subtotal=field("20.00", quote=quote), tax=field("2.00", quote=quote), total=field("99.00", quote=quote))
    second = candidate(subtotal=field("20.00", quote=quote))
    third = candidate(currency=field("USD", quote=quote), subtotal=field("20.00", quote=quote), tax=field("2.00", quote=quote), total=field("99.00", quote=quote), discount=field("0.00", quote="Discount 0.00"), other_charges=field("0.00", quote="Charges 0.00"))
    invoices = observe_extract(bundle, tmp_path, replay_for(bundle, first, second, third))["invoices"]
    assert invoices[0]["validation_policy"] == "source_backed_v2"
    assert invoices[0]["discount"] is invoices[0]["other_charges"] is None
    assert any(f["rule_id"] == "financial_terms_unverified" for f in invoices[0]["findings"])
    assert not any(f["rule_id"] == "total_mismatch" for f in invoices[0]["findings"])
    assert invoices[1]["tax"] is invoices[1]["total"] is invoices[1]["discount"] is None
    assert not any(f["rule_id"] == "total_mismatch" for f in invoices[1]["findings"])
    assert any(f["rule_id"] == "total_mismatch" for f in invoices[2]["findings"])


def test_source_id_and_region_membership_are_required_even_when_quote_exists_elsewhere(tmp_path):
    bundle = bundle_of("Invoice A Total 10.00", "Invoice B Total 10.00")
    observation = Observation(layout="two invoices", regions=[Region(id="r", source_ids=["s0"], purpose="first", layout="text"), Region(id="other", source_ids=["s1"], purpose="second", layout="text")], groups=[InvoiceGroup(id="g", region_ids=["r"], notes="first only"), InvoiceGroup(id="other-g", region_ids=["other"], notes="second")], ignored_source_ids=[], warnings=[])
    candidates = [candidate(total=field("10.00", source_id="missing", quote="10.00")), candidate(total=field("10.00", source_id="s1", quote="10.00")), candidate(total=field("10.00", source_id="s1", region_id="other", quote="10.00"))]
    result = observe_extract(bundle, tmp_path, replay_for(bundle, *candidates, observation=observation))
    assert all(invoice["total"] is None for invoice in result["invoices"])


def test_text_identity_substring_is_not_grounded(tmp_path):
    bundle = bundle_of("Invoice INV-100")
    invoice = observe_extract(bundle, tmp_path, replay_for(bundle, candidate(invoice_number=field("INV-10", quote="INV-10"))))["invoices"][0]
    assert invoice["invoice_number"] is None


@pytest.mark.parametrize("bad_observation", ["unknown_source", "unknown_group_region", "ignored_conflict", "duplicate_region", "duplicate_group"])
def test_invalid_observation_scope_fails_before_extraction(tmp_path, bad_observation):
    bundle = bundle_of("Invoice A")
    observation = observe_all(bundle)
    if bad_observation == "unknown_source":
        observation.regions[0].source_ids.append("outside-bundle")
    elif bad_observation == "unknown_group_region":
        observation.groups[0].region_ids.append("missing-region")
    elif bad_observation == "ignored_conflict":
        observation.ignored_source_ids.append("s0")
    elif bad_observation == "duplicate_region":
        observation.regions.append(observation.regions[0])
    else:
        observation.groups.append(observation.groups[0])
    replay = replay_for(bundle, observation=observation)
    with pytest.raises(ObservationError):
        observe_extract(bundle, tmp_path, replay)
    assert len(replay.calls) == 1
    assert json.loads((tmp_path / "observation_metadata.json").read_text())["status"] == "failed"


def test_coverage_preserves_unassigned_blocks_and_missing_groups(tmp_path):
    bundle = bundle_of("Invoice A", "unassigned invoice B")
    observation = observe_all(bundle)
    observation.regions[0].source_ids = ["s0"]
    result = observe_extract(bundle, tmp_path, replay_for(bundle, observation=observation))
    coverage = result["metadata"]["coverage"]
    assert coverage["uncovered_source_ids"] == ["s1"]
    assert coverage["unextracted_group_ids"] == ["g"]
    assert coverage["all_blocks_accounted_for"] is False
    assert result["metadata"]["needs_review"] is True
    assert result["metadata"]["finalization_blocked"] is True
    assert set(result["metadata"]["finalization_blocked_reasons"]) == {"unassigned_source_blocks", "unextracted_invoice_groups", "zero_supported_invoices"}
    assert result["invoices"] == []


@pytest.mark.parametrize("kind", ["numeric_value", "unknown_property", "multiple_numeric_value"])
def test_schema_is_strict_and_closed(kind):
    payload = candidate().model_dump()
    if kind == "numeric_value":
        payload["total"]["value"] = 10.0
    elif kind == "multiple_numeric_value":
        payload["line_items"] = [{name: field().model_dump() for name in LineCandidate.model_fields}]
        payload["line_items"][0]["quantity"]["value"] = 2
    else:
        payload["not_in_schema"] = "x"
    with pytest.raises(ValidationError):
        InvoiceCandidate.model_validate(payload)
    for schema in (Observation.model_json_schema(), Extraction.model_json_schema()):
        assert schema["additionalProperties"] is False
        assert all(definition.get("additionalProperties") is False for definition in schema["$defs"].values())


@pytest.mark.parametrize("provider", [None, AnthropicObservationProvider()])
def test_cloud_opt_in_required_before_sdk_construction(tmp_path, provider):
    with pytest.raises(ObservationError, match="explicit_opt_in"):
        observe_extract(bundle_of("Invoice A"), tmp_path, provider)


def test_direct_real_provider_enforces_cloud_opt_in():
    provider = AnthropicObservationProvider()
    bundle = bundle_of("Invoice A")
    with pytest.raises(ObservationError, match="explicit_opt_in"):
        provider.observe(bundle)
    with pytest.raises(ObservationError, match="explicit_opt_in"):
        provider.extract(bundle, observe_all(bundle))


@pytest.mark.parametrize("provider_kind", ["injected", "real"])
def test_oversize_fails_before_any_provider_call(tmp_path, monkeypatch, provider_kind):
    monkeypatch.setenv("OBSERVATION_MAX_CONTEXT_BYTES", "100")
    monkeypatch.setenv("ALLOW_CLOUD_EXTRACTION", "true")
    provider = Mock() if provider_kind == "injected" else AnthropicObservationProvider()
    with pytest.raises(ObservationError, match="context_too_large_no_chunking"):
        observe_extract(bundle_of("A" * 200), tmp_path, provider)
    if provider_kind == "injected":
        provider.observe.assert_not_called()
        provider.extract.assert_not_called()


def test_second_stage_context_guard_keeps_observation_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVATION_MAX_CONTEXT_BYTES", "20000")
    bundle = bundle_of("Invoice A")
    observation = observe_all(bundle)
    observation.layout = "X" * 22000
    replay = replay_for(bundle, observation=observation)
    with pytest.raises(ObservationError, match="context_too_large_no_chunking"):
        observe_extract(bundle, tmp_path, replay)
    assert len(replay.calls) == 1
    assert (tmp_path / "observation.json").exists()
    assert json.loads((tmp_path / "observation_metadata.json").read_text())["failed_stage"] == "extraction"


def mock_sdk(monkeypatch, responses=None, error=None):
    import anthropic
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    stream = Mock()
    stream.__enter__ = Mock(return_value=stream)
    stream.__exit__ = Mock(return_value=False)
    stream.get_final_message.side_effect = error or responses
    client.messages.stream.return_value = stream
    constructor = Mock(return_value=client)
    monkeypatch.setattr(anthropic, "Anthropic", constructor)
    return constructor, client


def response_for(model, content, stop_reason="end_turn"):
    return SimpleNamespace(model=model, stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=content)], usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 15, "output_tokens": 20, "cache_read_input_tokens": 5}))


def test_official_sdk_streams_both_stages_and_records_actual_fallback_model(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_CLOUD_EXTRACTION", "true")
    bundle = bundle_of("Invoice INV-9")
    observation = observe_all(bundle)
    extraction = Extraction(invoices=[candidate(invoice_number=field("INV-9", quote="Invoice INV-9"))], warnings=[])
    constructor, client = mock_sdk(monkeypatch, [response_for("claude-opus-5-5", observation.model_dump_json()), response_for("claude-opus-4-8", extraction.model_dump_json())])
    result = observe_extract(bundle, tmp_path)
    assert constructor.call_count == client.messages.stream.call_count == 2
    for call in constructor.call_args_list:
        assert call.kwargs["max_retries"] == 0
    for call in client.messages.stream.call_args_list:
        kwargs = call.kwargs
        assert kwargs["model"] == "claude-opus-5-5"
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"]["effort"] == "high"
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        assert kwargs["extra_body"] == {"fallbacks": "default"}
        assert kwargs["extra_headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"
        assert "tools" not in kwargs and "temperature" not in kwargs
    first = json.loads(client.messages.stream.call_args_list[0].kwargs["messages"][0]["content"])
    second = json.loads(client.messages.stream.call_args_list[1].kwargs["messages"][0]["content"])
    assert first == second["bundle"] == bundle
    assert second["observation"] == observation.model_dump()
    assert result["metadata"]["model"] == "claude-opus-4-8"
    assert result["metadata"]["extraction_runs"][1]["requested_model"] == "claude-opus-5-5"


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "tool_use", "pause_turn", "stop_sequence", None])
def test_non_end_turn_rejected_with_usage_and_without_retry(tmp_path, monkeypatch, stop_reason):
    monkeypatch.setenv("ALLOW_CLOUD_EXTRACTION", "true")
    constructor, client = mock_sdk(monkeypatch, [response_for("actual-model", "not valid JSON or safe text", stop_reason)])
    with pytest.raises(ObservationError, match="incomplete_or_refused"):
        observe_extract(bundle_of("Invoice A"), tmp_path)
    assert constructor.call_count == client.messages.stream.call_count == 1
    saved = json.loads((tmp_path / "observation_metadata.json").read_text())
    assert saved["extraction_runs"][0]["model"] == "actual-model"
    assert saved["extraction_runs"][0]["usage"]["input_tokens"] == 15
    assert "not valid JSON or safe text" not in json.dumps(saved)


def test_raw_provider_failure_text_is_not_persisted_or_raised(tmp_path):
    bundle = bundle_of("Invoice A")
    replay = replay_for(bundle)
    replay.observe = Mock(side_effect=RuntimeError("SECRET_KEY and raw provider body"))
    with pytest.raises(ObservationError) as caught:
        observe_extract(bundle, tmp_path, replay)
    assert caught.value.code == "observation_stage_failed"
    assert "SECRET" not in str(caught.value)
    assert "SECRET" not in (tmp_path / "observation_metadata.json").read_text()


def test_schema_error_is_not_retried_or_leaked(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_CLOUD_EXTRACTION", "true")
    _, client = mock_sdk(monkeypatch, [response_for("actual-model", '{"SECRET_KEY":"raw invalid payload"}')])
    with pytest.raises(ObservationError, match="provider_schema_invalid"):
        observe_extract(bundle_of("Invoice A"), tmp_path)
    assert client.messages.stream.call_count == 1
    assert "SECRET_KEY" not in (tmp_path / "observation_metadata.json").read_text()


def test_duplicate_source_ids_rejected_before_provider_call(tmp_path):
    bundle = bundle_of("Invoice A", "Invoice B")
    bundle["blocks"][1]["id"] = "s0"
    provider = Mock()
    with pytest.raises(ObservationError, match="duplicate_source_id"):
        observe_extract(bundle, tmp_path, provider)
    provider.observe.assert_not_called()


@pytest.mark.parametrize("condition,reason", [("upstream_flag", "source_finalization_blocked"), ("partial_metadata", "source_missing_or_partial"), ("partial_page", "source_pages_missing_or_partial"), ("missing_provenance", "source_provenance_missing"), ("ungrouped", "ungrouped_observation_regions")])
def test_structural_source_gaps_block_completion(tmp_path, condition, reason):
    bundle = bundle_of("Invoice INV-9")
    observation = observe_all(bundle)
    if condition == "upstream_flag":
        bundle["metadata"].update(finalization_blocked=True, finalization_blocked_reasons=["parser_incomplete"])
    elif condition == "partial_metadata":
        bundle["metadata"]["status"] = "partial"
    elif condition == "partial_page":
        bundle["pages"] = [{"page": 2, "status": "failed", "error": "local_page_failed"}]
    elif condition == "missing_provenance":
        bundle["blocks"][0]["location"] = {}
    elif condition == "ungrouped":
        observation.regions.append(Region(id="unassigned-region", source_ids=["s0"], purpose="unassigned", layout="text"))
    result = observe_extract(bundle, tmp_path, replay_for(bundle, candidate(invoice_number=field("INV-9", quote="Invoice INV-9")), observation=observation))
    assert result["metadata"]["finalization_blocked"] is True
    assert reason in result["metadata"]["finalization_blocked_reasons"]
    if condition == "upstream_flag":
        assert result["metadata"]["source_finalization_blocked_reasons"] == ["parser_incomplete"]


def test_reviewer_resolvable_field_rejection_does_not_block_document_completion(tmp_path):
    bundle = bundle_of("Invoice INV-9 Total -10.00")
    result = observe_extract(bundle, tmp_path, replay_for(bundle, candidate(invoice_number=field("INV-9", quote="Invoice INV-9"), total=field("10.00", quote="Total -10.00"))))
    assert result["invoices"][0]["total"] is None
    assert result["invoices"][0]["needs_review"] is True
    assert result["metadata"]["finalization_blocked"] is False


def test_shared_region_requires_explicit_group_membership(tmp_path):
    bundle = bundle_of("Invoice INV-A", "Invoice INV-B", "Vendor Acme")
    observation = Observation(layout="shared vendor two invoices", regions=[Region(id="a", source_ids=["s0"], purpose="first", layout="text"), Region(id="b", source_ids=["s1"], purpose="second", layout="text"), Region(id="shared", source_ids=["s2"], purpose="shared vendor", layout="heading")], groups=[InvoiceGroup(id="g", region_ids=["a", "shared"], notes="first"), InvoiceGroup(id="second", region_ids=["b", "shared"], notes="second")], ignored_source_ids=[], warnings=[])
    first = candidate(invoice_number=field("INV-A", "s0", "a", "Invoice INV-A"), supplier_name=field("Acme", "s2", "shared", "Vendor Acme"))
    second = candidate(invoice_number=field("INV-B", "s1", "b", "Invoice INV-B"), supplier_name=field("Acme", "s2", "shared", "Vendor Acme"))
    second.group_id = "second"
    result = observe_extract(bundle, tmp_path, replay_for(bundle, first, second, observation=observation))
    assert [invoice["supplier_name"] for invoice in result["invoices"]] == ["Acme", "Acme"]
    assert result["metadata"]["finalization_blocked"] is False


def test_explicit_line_financial_terms_are_grounded_and_validated(tmp_path):
    quote = "Invoice INV-9 Currency USD Basis Net Widget quantity 2 unit 10.00 discount 1.00 tax 2.00 line 19.00 Subtotal 19.00 Tax 2.00 Discount 0.00 Charges 0.00 Total 21.00"
    bundle = bundle_of(quote)
    row = LineCandidate(**{name: field(value, quote=quote) for name, value in {"description": "Widget", "quantity": "2", "unit_price": "10.00", "discount": "1.00", "tax": "2.00", "line_total": "19.00"}.items()})
    invoice = candidate(invoice_number=field("INV-9", quote=quote), currency=field("USD", quote=quote), line_amount_basis=field("Net", quote="Basis Net"), subtotal=field("19.00", quote="Subtotal 19.00"), tax=field("2.00", quote="Tax 2.00"), discount=field("0.00", quote="Discount 0.00"), other_charges=field("0.00", quote="Charges 0.00"), total=field("21.00", quote="Total 21.00"), line_items=[row])
    result = observe_extract(bundle, tmp_path, replay_for(bundle, invoice))
    output = result["invoices"][0]
    assert output["line_amount_basis"] == "net"
    assert output["line_items"][0]["discount"] == "1.00"
    assert output["line_items"][0]["tax"] == "2.00"
    assert not {"total_mismatch", "line_item_mismatch", "financial_terms_unverified", "line_terms_unverified", "line_tax_basis_unknown"} & {finding["rule_id"] for finding in output["findings"]}


def test_stage_specific_model_and_effort_config_are_forwarded_without_network(monkeypatch):
    monkeypatch.setenv("ALLOW_CLOUD_EXTRACTION", "true")
    monkeypatch.setenv("OBSERVATION_MODEL", "custom-layout-model")
    monkeypatch.setenv("EXTRACTION_MODEL", "custom-extract-model")
    monkeypatch.setenv("OBSERVATION_EFFORT", "medium")
    monkeypatch.setenv("EXTRACTION_EFFORT", "high")
    bundle = bundle_of("Invoice A")
    observation = observe_all(bundle)
    _, client = mock_sdk(monkeypatch, [response_for("actual-layout-model", observation.model_dump_json()), response_for("actual-extract-model", Extraction(invoices=[], warnings=[]).model_dump_json())])
    provider = AnthropicObservationProvider()
    provider.observe(bundle)
    provider.extract(bundle, observation)
    kwargs = [call.kwargs for call in client.messages.stream.call_args_list]
    assert [value["model"] for value in kwargs] == ["custom-layout-model", "custom-extract-model"]
    assert [value["output_config"]["effort"] for value in kwargs] == ["medium", "high"]


def test_gateway_compat_mode_omits_claude_only_options_and_accepts_fenced_json(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_CLOUD_EXTRACTION", "true")
    monkeypatch.setenv("ANTHROPIC_COMPAT_MODE", "true")
    bundle = bundle_of("Invoice INV-9")
    observation = observe_all(bundle)
    fenced = "```json\n" + observation.model_dump_json() + "\n```"
    _, client = mock_sdk(monkeypatch, [response_for("gateway-model", fenced, stop_reason="stop")])
    parsed, metadata = AnthropicObservationProvider().observe(bundle)
    assert parsed == observation and metadata["provider"] == "anthropic_compat"
    kwargs = client.messages.stream.call_args.kwargs
    assert not {"thinking", "output_config", "extra_headers", "extra_body"} & set(kwargs)
    assert "JSON schema" in kwargs["system"]
