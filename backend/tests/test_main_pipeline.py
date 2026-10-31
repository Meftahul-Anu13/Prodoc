"""Production routing/queue/approval tests. Model responses here are explicit replays."""
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from backend.app import parse_job, worker
from backend.app.artifacts import artifact_path, document_directory, new_run
from backend.app.db import Document, Invoice, Job, User, database, now
from backend.app.pipeline import validate_invoice
from backend.tests.test_api import env, headers, upload, process


def reviewed_invoice(**changes):
    return {'validation_policy': 'source_backed_v2', 'invoice_number': 'CHECK-1',
        'invoice_date': '2026-10-29', 'supplier_name': 'Synthetic Seller', 'customer_name': 'Synthetic Buyer',
        'currency': 'USD', 'subtotal': '100.00', 'tax': '10.00', 'discount': '0.00',
        'other_charges': '0.00', 'total': '110.00', 'line_amount_basis': 'net',
        'line_items': [{'description': 'Synthetic service', 'quantity': '2', 'unit_price': '50.00',
                        'discount': '0.00', 'tax': '10.00', 'line_total': '100.00'}],
        'evidence': {}, 'needs_review': True, **changes}


def test_default_main_pipeline_requires_consent_before_reading(monkeypatch, tmp_path):
    monkeypatch.delenv('EXTRACTION_PROVIDER')
    with pytest.raises(parse_job.ProcessingError, match='explicit_opt_in'):
        parse_job.process_document(tmp_path / 'missing.csv', 'missing.csv')


@pytest.mark.parametrize('filename', ['invoice.csv', 'invoice.xls', 'invoice.xlsx', 'invoice.pdf', 'invoice.png', 'invoice.jpg', 'invoice.tiff'])
def test_every_format_routes_to_observation_not_header_baseline(monkeypatch, tmp_path, filename):
    from backend.app import sources, observation
    monkeypatch.setenv('EXTRACTION_PROVIDER', 'anthropic')
    monkeypatch.setenv('ALLOW_CLOUD_EXTRACTION', 'true')
    bundle = {'pages': [{'page': 1, 'text': 'synthetic evidence'}], 'blocks': [], 'warnings': [], 'metadata': {'parser': 'replay'}}
    read = Mock(return_value=bundle)
    infer = Mock(return_value={'invoices': [], 'metadata': {'provider': 'anthropic', 'model': 'explicit-replay'}})
    monkeypatch.setattr(sources, 'read_sources', read)
    monkeypatch.setattr(observation, 'observe_extract', infer)
    monkeypatch.setattr(parse_job, 'process_file', Mock(side_effect=AssertionError('baseline must not run')))
    result = parse_job.process_document(tmp_path / 'stored-object', filename, tmp_path)
    read.assert_called_once_with(tmp_path / 'stored-object', filename, tmp_path)
    infer.assert_called_once_with(bundle, tmp_path)
    assert result['metadata']['requested_provider'] == 'anthropic'


def test_strict_financial_policy_does_not_assume_absent_adjustments_zero():
    assert not validate_invoice(reviewed_invoice())
    findings = validate_invoice(reviewed_invoice(discount=None, other_charges=None))
    assert any(f['rule_id'] == 'financial_terms_unverified' and f['severity'] == 'error' for f in findings)
    assert not any(f['rule_id'] == 'total_mismatch' for f in findings)


def test_currency_scale_unknown_currency_and_line_net_aggregate():
    bad_lines = reviewed_invoice(line_items=[{'quantity': '1', 'unit_price': '80.00', 'discount': '0.00', 'tax': '0.00', 'line_total': '80.00'}])
    assert any(f['rule_id'] == 'line_items_subtotal_mismatch' for f in validate_invoice(bad_lines))
    assert any(f['rule_id'] == 'unsupported_currency' for f in validate_invoice(reviewed_invoice(currency='ZZZ')))
    assert any(f['rule_id'] == 'currency_precision' for f in validate_invoice(reviewed_invoice(currency='JPY', total='110.25')))
    assert not any(f['rule_id'] == 'currency_precision' for f in validate_invoice(reviewed_invoice(currency='KWD', total='110.001')))
    assert any(f['rule_id'] == 'line_tax_basis_unknown' for f in validate_invoice(reviewed_invoice(line_amount_basis=None)))


def test_strict_financial_rejects_floats_ambiguous_date_and_gross_math():
    assert any(f['rule_id'] == 'invalid_numeric' for f in validate_invoice(reviewed_invoice(total=110.0)))
    assert any(f['rule_id'] == 'invalid_or_ambiguous_date' for f in validate_invoice(reviewed_invoice(invoice_date='03/04/2026')))
    gross = reviewed_invoice(line_amount_basis='gross', line_items=[{'quantity': '2', 'unit_price': '50.00', 'discount': '0.00', 'tax': '10.00', 'line_total': '110.00'}])
    assert not validate_invoice(gross)


def test_line_tax_aggregate_mismatch_cannot_pass_financial_acceptance():
    invoice = reviewed_invoice()
    invoice['line_items'][0]['tax'] = '0.00'
    assert any(f['rule_id'] == 'line_items_tax_mismatch' and f['severity'] == 'error' for f in validate_invoice(invoice))


@pytest.mark.parametrize('value', ['not-a-number', '0', '870'])
def test_invalid_child_timeout_fails_before_starting_parser(monkeypatch, tmp_path, value):
    monkeypatch.setenv('PROCESS_TIMEOUT_SECONDS', value)
    spawn = Mock(side_effect=AssertionError('must not start parser'))
    monkeypatch.setattr(worker.subprocess, 'run', spawn)
    with pytest.raises(worker.ParserFailure) as captured:
        worker.isolated_parse(tmp_path / 'stored.csv', 'messy.csv')
    assert captured.value.code == 'invalid_processing_timeout'
    assert captured.value.permanent and captured.value.artifact_run_id
    spawn.assert_not_called()


def test_expired_ai_lease_never_rebills(tmp_path):
    engine, sessions = database('sqlite:///' + str(tmp_path / 'lease.db'))
    with sessions() as session:
        user = User(email='lease@example.test', password_hash='unused')
        session.add(user)
        session.flush()
        doc = Document(owner_id=user.id, filename='messy.csv', storage_key='s.csv', sha256='f' * 64,
            status='extracting', extraction_metadata={'requested_provider': 'anthropic'})
        session.add(doc)
        session.flush()
        job = Job(document_id=doc.id, status='extracting', attempts=1, lease_until=now() - timedelta(seconds=1))
        session.add(job)
        session.commit()
        assert worker.claim(session) is None
        session.refresh(job)
        assert job.status == 'failed'
        assert job.error_code == 'cloud_processing_outcome_unknown'
    engine.dispose()


def test_ai_parser_crash_does_not_retry_paid_work(env, monkeypatch):
    app, client = env
    monkeypatch.setenv('EXTRACTION_PROVIDER', 'anthropic')
    monkeypatch.setattr(worker, 'isolated_parse', Mock(side_effect=RuntimeError('PRIVATE DETAILS')))
    auth = headers(client)
    identifier = upload(client, auth).json()['document_id']
    process(app)
    result = client.get(f'/api/v1/documents/{identifier}/status', headers=auth).json()
    assert result['status'] == 'failed'
    assert result['error_code'] == 'cloud_processing_outcome_unknown'
    assert 'PRIVATE' not in json.dumps(result)


def test_coverage_blocks_finalization_and_private_artifacts_are_owner_scoped(env, monkeypatch):
    app, client = env
    auth, other = headers(client), headers(client, 'other@example.test')
    identifier = upload(client, auth).json()['document_id']
    with app.state.sessions() as session:
        doc = session.get(Document, identifier)
        key = doc.storage_key
    run_id, directory = new_run(app.state.storage / key)
    (directory / 'source.json').write_text('{"blocks": []}', encoding='utf-8')
    (directory / 'observation_metadata.json').write_text('{"status": "replay"}', encoding='utf-8')
    invoice = reviewed_invoice()
    invoice['findings'] = validate_invoice(invoice)
    monkeypatch.setattr(worker, 'isolated_parse', lambda p, f: {'pages': [], 'invoices': [invoice],
        'metadata': {'provider': 'anthropic', 'finalization_blocked': True, 'artifact_run_id': run_id}})
    process(app)
    results = client.get(f'/api/v1/documents/{identifier}/results', headers=auth).json()
    inv = results['invoices'][0]
    response = client.post(f"/api/v1/invoices/{inv['id']}/finalize", headers=auth, json={'version': 1})
    assert response.status_code == 409
    artifact = f'/api/v1/documents/{identifier}/artifacts/source.json'
    assert client.get(artifact, headers=auth).json() == {'blocks': []}
    assert client.get(f'/api/v1/documents/{identifier}/artifacts/observation_metadata.json', headers=auth).json() == {'status': 'replay'}
    assert client.get(artifact, headers=other).status_code == 404
    assert client.get(artifact).status_code == 401
    assert client.get(f'/api/v1/documents/{identifier}/artifacts/secret.json', headers=auth).status_code == 404
    assert client.delete(f'/api/v1/documents/{identifier}', headers=auth).status_code == 200
    assert not document_directory(app.state.storage, key).exists()


def test_transposed_csv_through_native_reader_two_stage_replay_and_api_review(env, monkeypatch):
    """Real CSV reader/grounding/SQL/API; only model answers and process boundary replayed."""
    from backend.app import observation
    app, client = env
    monkeypatch.setenv('EXTRACTION_PROVIDER', 'anthropic')
    monkeypatch.setenv('ALLOW_CLOUD_EXTRACTION', 'true')
    calls = []

    class LayoutReplay:
        def observe(self, bundle):
            calls.append('observation')
            regions = [{'id': group, 'purpose': 'invoice', 'layout': 'transposed columns',
                'source_ids': [block['id'] for block in bundle['blocks'] if block['location']['column'] in {1, column}]}
                for group, column in [('a', 2), ('b', 3)]]
            return observation.Observation(layout='Explicit offline replay: transposed invoices', regions=regions,
                groups=[{'id': group, 'region_ids': [group], 'notes': 'replay'} for group in ('a', 'b')],
                ignored_source_ids=[], warnings=[]), {'provider': 'offline-replay', 'model': 'layout-replay'}

        def extract(self, bundle, layout):
            calls.append('extraction')
            cells = {(block['location']['row'], block['location']['column']): block for block in bundle['blocks']}
            invoices = []
            for group, column in [('a', 2), ('b', 3)]:
                def field(row):
                    block = cells[row, column]
                    return {'value': block['text'], 'quote': block['text'], 'source_id': block['id'], 'region_id': group}
                fields = dict(zip(('invoice_number', 'invoice_date', 'supplier_name', 'customer_name', 'currency',
                    'subtotal', 'tax', 'discount', 'other_charges', 'total', 'line_amount_basis'), range(2, 13)))
                invoices.append({'group_id': group, **{name: field(row) for name, row in fields.items()},
                    'line_items': [{name: field(row) for name, row in zip(
                        ('description', 'quantity', 'unit_price', 'discount', 'tax', 'line_total'), range(13, 19))}]})
            return observation.Extraction(invoices=invoices, warnings=[]), {'provider': 'offline-replay', 'model': 'extraction-replay'}

    monkeypatch.setattr(observation, 'AnthropicObservationProvider', LayoutReplay)
    def in_process_replay(path, filename):
        run_id, directory = new_run(path)
        result = parse_job.process_document(path, filename, directory)
        result['metadata']['artifact_run_id'] = run_id
        return result
    monkeypatch.setattr(worker, 'isolated_parse', in_process_replay)
    text = ('Field,Record A,Record B\nInvoice,PIPE-A,PIPE-B\nDate,2026-10-29,2026-10-29\n'
        'Supplier,Synthetic Seller,Synthetic Seller\nCustomer,Synthetic Buyer,Synthetic Buyer\n'
        'Currency,USD,USD\nSubtotal,100.00,200.00\nTax,10.00,20.00\nDiscount,0.00,0.00\n'
        'Charges,0.00,0.00\nTotal,110.00,220.00\nBasis,net,net\nDescription,Service A,Service B\n'
        'Quantity,2,4\nPrice,50.00,50.00\nDiscount,0.00,0.00\nTax,10.00,20.00\nLine total,100.00,200.00\n')
    auth = headers(client)
    response = client.post('/api/v1/documents', headers=auth, files={'file': ('transposed.csv', text.encode(), 'text/csv')})
    assert response.status_code == 202
    identifier = response.json()['document_id']
    process(app)
    result = client.get(f'/api/v1/documents/{identifier}/results', headers=auth).json()
    assert calls == ['observation', 'extraction']
    assert result['document']['status'] == 'needs_review'
    assert result['document']['metadata']['finalization_blocked'] is False
    assert len(result['invoices']) == 2
    assert {item['data']['invoice_number'] for item in result['invoices']} == {'PIPE-A', 'PIPE-B'}
    for item in result['invoices']:
        assert not any(f['severity'] == 'error' for f in item['data']['findings'])
        assert item['data']['evidence']['total']['cell'] in {'B11', 'C11'}
        approved = client.post(f"/api/v1/invoices/{item['id']}/finalize", headers=auth, json={'version': item['version']})
        assert approved.status_code == 200
    assert client.get(f'/api/v1/documents/{identifier}/status', headers=auth).json()['status'] == 'completed'
    assert client.get(f'/api/v1/documents/{identifier}/artifacts/observation_metadata.json', headers=auth).json()['model'] == 'extraction-replay'


def test_artifact_reference_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        artifact_path(tmp_path, 'key', '../outside', 'source.json')
    with pytest.raises(ValueError):
        artifact_path(tmp_path, 'key', '0' * 32, '../secret')
