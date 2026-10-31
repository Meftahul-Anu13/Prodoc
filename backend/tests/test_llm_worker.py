from datetime import timedelta

import pytest
from hypothesis import given, strategies as st
from sqlalchemy import select

from backend.app.db import Audit, Document, Job, User, database, now
from backend.app.llm import Candidate, GroundedField, LineCandidate, enhance, grounded
from backend.app.search import rrf
from backend.app.worker import claim, tick


def test_llm_grounding_rejects_fabrication_and_location():
    pages = [{'page': 1, 'text': 'Invoice INV-7 total USD 20.00'}]
    assert grounded(GroundedField(value='20.00', source_text='total USD 20.00', page=1), pages)
    assert not grounded(GroundedField(value='200.00', source_text='total USD 20.00', page=1), pages)
    assert not grounded(GroundedField(value='20.00', source_text='total USD 20.00', page=2), pages)
    assert not grounded(GroundedField(value='20.00', source_text='invented total 20.00', page=1), pages)


def test_llm_provider_output_is_grounded_and_records_usage():
    values = {'invoice_number': 'INV-7', 'invoice_date': None, 'supplier_name': 'Imaginary supplier', 'customer_name': None, 'subtotal': None, 'tax': None, 'total': '20.00', 'currency': 'USD'}
    fields = {k: GroundedField(value=v, source_text='Invoice INV-7 total USD 20.00' if k != 'supplier_name' else 'Imaginary supplier', page=1) for k, v in values.items()}
    class ReplayProvider:
        def extract(self, pages):
            return Candidate(**fields, line_items=[]), {'provider': 'test-replay', 'usage': {'input_tokens': 10, 'output_tokens': 5}}
    source = {'pages': [{'page': 1, 'text': 'Invoice INV-7 total USD 20.00'}], 'invoices': [{'page_start': 1, 'page_end': 1, 'evidence': {}}], 'metadata': {}}
    result = enhance(source, ReplayProvider())
    assert result['invoices'][0]['supplier_name'] is None
    assert result['invoices'][0]['total'] == '20.00'
    assert any(f['rule_id'] == 'ungrounded_field' for f in result['invoices'][0]['findings'])
    assert result['metadata']['extraction_runs'][0]['provider'] == 'test-replay'


def test_llm_line_schema_is_closed_and_line_values_are_grounded():
    schema = Candidate.model_json_schema()
    assert all(definition.get('additionalProperties') is False for definition in schema['$defs'].values())
    quote = 'Invoice INV-7 Widget 2 10.00 20.00 USD'
    fields = {name: GroundedField(value=None, source_text='', page=1) for name in Candidate.model_fields if name != 'line_items'}
    line = LineCandidate(**{name: GroundedField(value=value, source_text=quote, page=1) for name, value in
        {'description': 'Widget', 'quantity': '2', 'unit_price': '10.00', 'line_total': '999.00'}.items()})
    class ReplayProvider:
        def extract(self, pages):
            return Candidate(**fields, line_items=[line]), {'provider': 'test-replay'}
    result = enhance({'pages': [{'page': 1, 'text': quote}], 'invoices': [{'page_start': 1, 'page_end': 1}], 'metadata': {}}, ReplayProvider())
    row = result['invoices'][0]['line_items'][0]
    assert row['description'] == 'Widget'
    assert row['line_total'] is None
    assert row['evidence']['line_total']['grounded'] is False


def test_cloud_requires_opt_in(monkeypatch):
    monkeypatch.setenv('EXTRACTION_PROVIDER', 'anthropic')
    monkeypatch.delenv('ALLOW_CLOUD_EXTRACTION', raising=False)
    with pytest.raises(ValueError, match='explicit_opt_in'):
        enhance({'invoices': []})


def test_claim_recovers_expired_lease_and_caps_retries(tmp_path):
    engine, sessions = database('sqlite:///' + str(tmp_path / 'queue.db'))
    with sessions() as s:
        user = User(email='queue@test.example', password_hash='not-used', role='uploader')
        s.add(user)
        s.flush()
        doc = Document(owner_id=user.id, filename='a.csv', sha256='a'*64, storage_key='a.csv', status='extracting')
        s.add(doc)
        s.flush()
        job = Job(document_id=doc.id, status='extracting', attempts=1, lease_until=now()-timedelta(seconds=1))
        s.add(job)
        s.commit()
        assert claim(s) is None
        s.refresh(job)
        assert job.status == 'retrying'
        j = claim(s)
        assert j.id == job.id and j.attempts == 2
        j.status, j.attempts = 'extracting', 3
        j.lease_until = now()-timedelta(seconds=1)
        s.commit()
        assert claim(s) is None
        s.refresh(j)
        assert j.status == 'failed'
    engine.dispose()


@given(st.lists(st.integers(0, 100), unique=True, max_size=30))
def test_rrf_preserves_ranking_for_single_input(ids):
    assert rrf(ids) == ids
