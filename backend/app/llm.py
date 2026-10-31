"""Optional structured LLM extraction. Baseline mode never calls a provider."""
import json
import os
import re
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

FIELDS = ('invoice_number', 'invoice_date', 'supplier_name', 'customer_name', 'subtotal', 'tax', 'total', 'currency')
PROMPT_VERSION = 'extraction-v1'


class GroundedField(BaseModel):
    model_config = ConfigDict(extra='forbid')
    value: str | None
    source_text: str
    page: int = Field(ge=1)


class LineCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    description: GroundedField
    quantity: GroundedField
    unit_price: GroundedField
    line_total: GroundedField


class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    invoice_number: GroundedField
    invoice_date: GroundedField
    supplier_name: GroundedField
    customer_name: GroundedField
    subtotal: GroundedField
    tax: GroundedField
    total: GroundedField
    currency: GroundedField
    # Every item is individually grounded too; no opaque unvalidated row dictionaries.
    line_items: list[LineCandidate]


class ExtractionProvider(Protocol):
    def extract(self, pages: list[dict]) -> tuple[Candidate, dict]: ...


class AnthropicProvider:
    def extract(self, pages):
        import anthropic
        prompt = (Path(__file__).resolve().parents[2] / 'prompts' / 'extraction-v1.txt')
        if not prompt.exists():
            prompt = Path(__file__).resolve().parents[2].parent / 'prompts' / 'extraction-v1.txt'
        system = prompt.read_text(encoding='utf-8')
        model = os.getenv('LLM_MODEL', 'claude-opus-5-5')
        # No tools and no image/original uploads. Only selected source segments are sent.
        source = json.dumps([{'page': p.get('page', p.get('page_number', index + 1)), 'text': p.get('text', '')} for index, p in enumerate(pages)], ensure_ascii=False)
        if len(source) > 60000:
            raise ValueError('llm_segment_too_large')
        with anthropic.Anthropic(timeout=60, max_retries=1) as client:
            with client.messages.stream(model=model, max_tokens=16000, system=system,
                    messages=[{'role': 'user', 'content': '<untrusted_document>\n' + source + '\n</untrusted_document>'}],
                    output_config={'format': {'type': 'json_schema', 'schema': Candidate.model_json_schema()}},
                    extra_headers={'anthropic-beta': 'server-side-fallback-2026-07-01'},
                    extra_body={'fallbacks': 'default'}) as stream:
                response = stream.get_final_message()
        if response.stop_reason not in {'end_turn'}:
            raise ValueError('llm_incomplete_or_refused')
        raw = ''.join(b.text for b in response.content if b.type == 'text')
        parsed = Candidate.model_validate_json(raw)
        return parsed, {'provider': 'anthropic', 'model': response.model, 'requested_model': model, 'prompt_version': PROMPT_VERSION,
                        'schema_version': '1', 'usage': response.usage.model_dump(), 'temperature': 'not_sent_model_default',
                        'fallback_policy': 'provider_default'}


def normalize(value):
    return re.sub(r'[^\w]', '', str(value or '').casefold())


def grounded(field, pages):
    if field.value is None:
        return True
    candidates = [p for index, p in enumerate(pages) if p.get('page', p.get('page_number', index + 1)) == field.page]
    quote = normalize(field.source_text)
    value = normalize(field.value)
    return bool(quote and value and value in quote and any(quote in normalize(p.get('text')) for p in candidates))


def enhance(result, provider=None):
    from .pipeline import validate_invoice
    if provider is None:
        if os.getenv('EXTRACTION_PROVIDER') != 'anthropic':
            raise ValueError('unsupported_extraction_provider')
        if os.getenv('ALLOW_CLOUD_EXTRACTION', 'false').lower() != 'true':
            raise ValueError('cloud_extraction_requires_explicit_opt_in')
        provider = AnthropicProvider()
    invoices = []
    runs = []
    for baseline in result.get('invoices', []):
        # Spreadsheet mapping is deterministic and never incurs per-row LLM calls.
        if any(ev.get('sheet') for ev in baseline.get('evidence', {}).values() if isinstance(ev, dict)):
            invoices.append(baseline)
            continue
        start, end = baseline.get('page_start', 1), baseline.get('page_end', len(result.get('pages', [])))
        pages = [p for n, p in enumerate(result.get('pages', []), 1) if start <= p.get('page', p.get('page_number', n)) <= end]
        candidate = None
        for attempt in range(2):
            try:
                candidate, metadata = provider.extract(pages)
                runs.append({**metadata, 'attempt': attempt + 1})
                break
            except (ValidationError, json.JSONDecodeError):
                if attempt == 1:
                    raise ValueError('llm_schema_invalid')
        data = {'page_start': start, 'page_end': end, 'evidence': {}, 'line_items': [], 'needs_review': True}
        rejected = []
        for name in FIELDS:
            field = getattr(candidate, name)
            good = grounded(field, pages)
            data[name] = field.value if good else None
            data['evidence'][name] = {'source_text': field.source_text, 'page': field.page, 'method': 'llm', 'grounded': good}
            if not good:
                rejected.append(name)
        for row in candidate.line_items:
            values = {}
            evidence = {}
            for name in LineCandidate.model_fields:
                field = getattr(row, name)
                good = grounded(field, pages)
                values[name] = field.value if good else None
                evidence[name] = {'source_text': field.source_text, 'page': field.page, 'grounded': good}
                if not good:
                    rejected.append('line_items.' + name)
            data['line_items'].append({**values, 'evidence': evidence})
        data['findings'] = validate_invoice(data)
        if rejected:
            data['findings'].append({'rule_id': 'ungrounded_field', 'severity': 'needs_review', 'message': 'Unsupported candidate values were set to null', 'fields': rejected, 'rule_version': '1'})
        invoices.append(data)
    return {**result, 'invoices': invoices, 'metadata': {**result.get('metadata', {}), 'extraction_runs': runs, 'provider': 'anthropic'}}
