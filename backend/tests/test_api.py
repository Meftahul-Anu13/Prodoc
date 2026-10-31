import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from backend.app.db import Audit, Document, Invoice, Job, User
from backend.app.main import create_app
from backend.app.security import hasher
from backend.app.worker import tick


@pytest.fixture
def env(tmp_path):
    app = create_app('sqlite:///' + str(tmp_path / 'test.db'), tmp_path / 'objects', 'test-only-key-' * 5)
    with app.state.sessions() as s:
        for email, role in [('owner@example.test', 'reviewer'), ('other@example.test', 'uploader'), ('admin@example.test', 'admin')]:
            s.add(User(email=email, role=role, password_hash=hasher.hash('secure-test-password')))
        s.commit()
    with TestClient(app) as client:
        yield app, client


def headers(client, email='owner@example.test'):
    response = client.post('/api/v1/auth/login', json={'email': email, 'password': 'secure-test-password'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


def csv_data(number='INV-42', total='110.00'):
    return f'invoice_number,invoice_date,supplier_name,customer_name,subtotal,tax,total,currency\n{number},2026-01-01,Acme Ltd,Buyer Ltd,100.00,10.00,{total},USD\n'.encode()


def upload(client, auth, data=None, filename='invoice.csv', idem=None):
    return client.post('/api/v1/documents', files={'file': (filename, data or csv_data())}, headers={**auth, **({'Idempotency-Key': idem} if idem else {})})


def process(app):
    assert tick(app.state.sessions, app.state.storage)


def test_observation_result_and_safe_failure_are_persisted_by_worker(env, monkeypatch):
    from backend.app import worker
    app, client = env
    auth = headers(client)
    # Explicitly synthetic replay verifies API/queue persistence, NOT live inference.
    replay = {'pages': [{'page': 1, 'text': 'Total 105.00'}],
        'invoices': [{'invoice_number': 'REPLAY-9', 'total': '105.00', 'line_items': [],
            'evidence': {'total': {'source_id': 'b1', 'source_text': '105.00', 'page': 1}},
            'findings': [], 'needs_review': True}],
        'metadata': {'provider': 'anthropic', 'model': 'offline-replay-not-a-served-model',
            'observation': {'layout_summary': 'synthetic scattered invoice'}, 'coverage': {'unassigned_source_ids': []}}}
    monkeypatch.setattr(worker, 'isolated_parse', lambda path, filename: replay)
    identifier = upload(client, auth, data=b'\x89PNG\r\n\x1a\nsynthetic', filename='observed.png').json()['document_id']
    process(app)
    result = client.get(f'/api/v1/documents/{identifier}/results', headers=auth).json()
    assert result['document']['metadata']['provider'] == 'anthropic'
    assert result['document']['metadata']['observation']['layout_summary'] == 'synthetic scattered invoice'
    assert result['document']['status'] == 'needs_review'
    assert result['invoices'][0]['data']['total'] == '105.00'
    assert result['invoices'][0]['data']['evidence']['total']['source_id'] == 'b1'
    with app.state.sessions() as session:
        assert session.scalar(select(Invoice).where(Invoice.document_id == identifier)).version == 1
    def fail(path, filename):
        raise worker.ParserFailure('mineru_configuration_required', True)
    monkeypatch.setattr(worker, 'isolated_parse', fail)
    failed_id = upload(client, auth, data=b'\x89PNG\r\n\x1a\ndifferent', filename='unconfigured.png').json()['document_id']
    process(app)
    status = client.get(f'/api/v1/documents/{failed_id}/status', headers=auth).json()
    assert status['status'] == 'failed'
    assert status['error_code'] == 'mineru_configuration_required'


def test_end_to_end_correction_version_audit_search_delete(env):
    app, client = env
    auth = headers(client)
    response = upload(client, auth)
    assert response.status_code == 202, response.text
    identifier = response.json()['document_id']
    process(app)
    result = client.get(f'/api/v1/documents/{identifier}/results', headers=auth).json()
    assert result['document']['status'] == 'needs_review', result
    assert len(result['invoices']) == 1, result
    invoice = result['invoices'][0]
    assert invoice['data']['total'] == '110.00'
    correction = {'version': 1, 'changes': {'customer_name': 'Corrected Buyer'}, 'reason': 'Checked original buyer identity'}
    changed = client.post(f"/api/v1/invoices/{invoice['id']}/corrections", json=correction, headers=auth)
    assert changed.status_code == 200, changed.text
    assert changed.json()['version'] == 2
    conflict = client.post(f"/api/v1/invoices/{invoice['id']}/corrections", json=correction, headers=auth)
    assert conflict.status_code == 409
    history = client.get(f'/api/v1/documents/{identifier}/audit', headers=auth).json()
    assert len(history['versions']) == 2
    assert any(item['action'] == 'corrected' for item in history['items'])
    assert client.get('/api/v1/search?q=Corrected', headers=auth).json()['items']
    assert client.post('/api/v1/search/semantic', json={'query': 'Corrected'}, headers=auth).json()['mode'] == 'keyword_fallback'
    finalized = client.post(f"/api/v1/invoices/{invoice['id']}/finalize", json={'version': 2}, headers=auth)
    assert finalized.status_code == 200, finalized.text
    assert client.get(f'/api/v1/documents/{identifier}', headers=auth).json()['status'] == 'completed'
    assert client.delete(f'/api/v1/documents/{identifier}', headers=auth).status_code == 200
    assert client.get(f'/api/v1/documents/{identifier}', headers=auth).status_code == 404
    with app.state.sessions() as s:
        assert not s.scalars(select(Invoice)).all()
        assert s.scalars(select(Audit).where(Audit.action == 'deleted')).all()


def test_auth_owner_rbac_and_semantic_reauthorization(env, monkeypatch):
    app, client = env
    assert upload(client, {}).status_code == 401
    owner, other = headers(client), headers(client, 'other@example.test')
    identifier = upload(client, owner).json()['document_id']
    process(app)
    assert client.get(f'/api/v1/documents/{identifier}', headers=other).status_code == 404
    assert not client.get('/api/v1/search?q=Acme', headers=other).json()['items']
    monkeypatch.setattr('backend.app.search.semantic_ids', lambda *args: [identifier])
    assert not client.post('/api/v1/search/semantic', json={'query': 'Acme'}, headers=other).json()['items']
    own = upload(client, other, csv_data('OTHER')).json()['document_id']
    process(app)
    invoice = client.get(f'/api/v1/documents/{own}/results', headers=other).json()['invoices'][0]
    assert client.post(f"/api/v1/invoices/{invoice['id']}/corrections", json={'version': 1, 'changes': {'total': '1'}, 'reason': 'test update'}, headers=other).status_code == 403


def test_idempotency_file_signature_corrupt_and_retry(env):
    app, client = env
    auth = headers(client)
    first = upload(client, auth, idem='same').json()
    second = upload(client, auth, idem='same').json()
    assert first['document_id'] == second['document_id']
    assert upload(client, auth, csv_data('DIFFERENT'), idem='same').status_code == 409
    assert upload(client, auth, b'not really a PDF', 'invoice.pdf').status_code == 422
    assert upload(client, auth, b'hello', 'run.exe').status_code == 415
    corrupt = upload(client, auth, b'%PDF-1.7\nnot a valid document', 'broken.pdf').json()['document_id']
    process(app)
    process(app)
    assert client.get(f'/api/v1/documents/{corrupt}', headers=auth).json()['status'] == 'failed'
    assert client.post(f'/api/v1/documents/{corrupt}/reprocess', headers=auth).status_code == 202
    process(app)
    assert client.get(f'/api/v1/documents/{corrupt}', headers=auth).json()['status'] == 'failed'


def test_duplicate_reconciliation_batch_and_append_only(env):
    app, client = env
    auth = headers(client)
    left = upload(client, auth).json()['document_id']
    right = upload(client, auth, csv_data(total='111.00')).json()['document_id']
    process(app)
    process(app)
    li = client.get(f'/api/v1/documents/{left}/results', headers=auth).json()['invoices'][0]
    ri = client.get(f'/api/v1/documents/{right}/results', headers=auth).json()['invoices'][0]
    assert ri['data']['duplicates']
    diff = client.post('/api/v1/reconciliations/run', json={'left_invoice_id': li['id'], 'right_invoice_id': ri['id']}, headers=auth)
    assert diff.status_code == 200, diff.text
    assert diff.json()['status'] == 'mismatch'
    invalid = client.post(f"/api/v1/invoices/{ri['id']}/finalize", json={'version': 1}, headers=auth)
    assert invalid.status_code == 409
    batch = client.post('/api/v1/documents/batch', headers=auth, files=[('files', ('a.csv', csv_data('A'))), ('files', ('b.exe', b'invalid'))])
    assert batch.status_code == 202
    assert 'error' in batch.json()['items'][1]
    with app.state.sessions() as s:
        with pytest.raises(Exception, match='append-only'):
            s.execute(text("UPDATE audit_events SET action='tampered'"))
        s.rollback()


def test_invalid_correction_and_request_errors(env):
    app, client = env
    auth = headers(client)
    identifier = upload(client, auth).json()['document_id']
    process(app)
    i = client.get(f'/api/v1/documents/{identifier}/results', headers=auth).json()['invoices'][0]
    assert client.post(f"/api/v1/invoices/{i['id']}/corrections", json={'version': 1, 'changes': {'total': 1.2}, 'reason': 'test'}, headers=auth).status_code == 422
    error = client.post('/api/v1/auth/login', json={})
    assert error.status_code == 422 and error.json()['error']['code'] == 'invalid_request'
    assert 'X-Request-ID' in error.headers


def test_semantic_ids_apply_floor_and_gap_cutoffs(monkeypatch):
    from types import SimpleNamespace
    import numpy as np
    from backend.app import search
    points = [SimpleNamespace(score=score, payload={'document_id': doc}) for doc, score in
              [('strong', 0.70), ('strong', 0.60), ('near', 0.67), ('weak', 0.60), ('noise', 0.45)]]
    fake = SimpleNamespace(query_points=lambda *a, **k: SimpleNamespace(points=points))
    monkeypatch.setenv('SEMANTIC_ENABLED', 'true')
    monkeypatch.setattr(search, 'client', lambda: fake)
    monkeypatch.setattr(search, 'embedding_model', lambda: SimpleNamespace(query_embed=lambda q: iter([np.zeros(3)])))
    assert search.semantic_ids('q', 'owner') == ['strong', 'near']
    points[:] = [SimpleNamespace(score=0.50, payload={'document_id': 'unrelated'})]
    assert search.semantic_ids('q', 'owner') == []


def test_smart_search_exact_fuzzy_normalized_and_snippets(env):
    app, client = env
    auth, other = headers(client), headers(client, 'other@example.test')
    with app.state.sessions() as s:
        owner = s.scalar(select(User).where(User.email == 'owner@example.test'))
        stranger = s.scalar(select(User).where(User.email == 'other@example.test'))
        docs = {}
        for name, who, body in [('north.pdf', owner, 'Northwind Office Supply Ltd.\nInvoice No: NW-2026-0417\nErgonomic office chair 189.00\nTotal Due 15,362.70'),
                                ('xyz.jpg', owner, 'XYZ Company\nConsulting Service 5 $200.00\nShipping Service 1 $25.00'),
                                ('secret.pdf', stranger, 'Northwind confidential')]:
            d = Document(owner_id=who.id, filename=name, sha256=name * 4, storage_key=name, status='needs_review', search_text=body, extraction_metadata={})
            s.add(d)
            s.flush()
            docs[name] = d.id
        s.commit()

    def smart(query, who=auth, modes=('keyword',)):
        response = client.post('/api/v1/search/smart', json={'query': query, 'modes': list(modes)}, headers=who)
        assert response.status_code == 200
        return response.json()['items']

    exact = smart('northwind chair')  # words in any order
    assert [i['filename'] for i in exact] == ['north.pdf'] and exact[0]['matches'][0]['type'] == 'exact'
    match = exact[0]['matches'][0]
    assert any(match['snippet'][a:b].lower() == 'northwind' for a, b in match['highlights'])
    typo = smart('Nortwind')
    assert [i['filename'] for i in typo] == ['north.pdf'] and typo[0]['matches'][0]['type'] == 'fuzzy'
    assert smart('consluting')[0]['filename'] == 'xyz.jpg'
    assert [i['filename'] for i in smart('NW20260417')] == ['north.pdf']   # compact invoice number
    assert [i['filename'] for i in smart('nw 2026 0417')] == ['north.pdf']
    assert [i['filename'] for i in smart('15362.70')] == ['north.pdf']     # amount without separator
    assert smart('banana smoothie') == []
    assert [i['filename'] for i in smart('Northwind', other)] == ['secret.pdf']  # owner-scoped
    assert all(i['filename'] != 'secret.pdf' for i in smart('Northwind'))
    client.delete(f"/api/v1/documents/{docs['north.pdf']}", headers=auth)
    assert smart('Northwind') == []


def test_smart_search_labels_semantic_matches_with_their_line(env, monkeypatch):
    app, client = env
    auth = headers(client)
    with app.state.sessions() as s:
        owner = s.scalar(select(User).where(User.email == 'owner@example.test'))
        d = Document(owner_id=owner.id, filename='xyz.jpg', sha256='x' * 64, storage_key='x', status='needs_review',
                     search_text='XYZ Company\nShipping Service 1 $25.00', extraction_metadata={})
        s.add(d)
        s.commit()
        identifier = d.id
    from backend.app import search
    offset = next(start for start, chunk in search.chunks('XYZ Company\nShipping Service 1 $25.00'))
    monkeypatch.setattr(search, 'semantic_hits', lambda *a: [{'document_id': identifier, 'score': 0.71, 'offset': offset}])
    items = client.post('/api/v1/search/smart', json={'query': 'delivery charges'}, headers=auth).json()['items']
    assert [m['type'] for m in items[0]['matches']] == ['semantic'] and 'Shipping Service' in items[0]['matches'][0]['snippet']


def test_smart_search_skips_semantic_for_identifier_queries(env, monkeypatch):
    app, client = env
    calls = []
    monkeypatch.setattr('backend.app.search.semantic_hits', lambda *a: calls.append(a) or [])
    auth = headers(client)
    client.post('/api/v1/search/smart', json={'query': 'NW-2026-0417 15362.70'}, headers=auth)
    assert calls == []
    client.post('/api/v1/search/smart', json={'query': 'NW-2026-0417', 'modes': ['semantic']}, headers=auth)
    client.post('/api/v1/search/smart', json={'query': 'office chairs'}, headers=auth)
    assert len(calls) == 2  # explicit semantic mode and word queries still use meaning search


def test_page_preview_is_owner_scoped_and_bounded(env):
    import fitz
    app, client = env
    owner, other = headers(client), headers(client, 'other@example.test')
    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 40), 'Invoice page')
        data = pdf.tobytes()
    identifier = upload(client, owner, data, 'scan.pdf').json()['document_id']
    response = client.get(f'/api/v1/documents/{identifier}/preview/1', headers=owner)
    assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
    assert response.content.startswith(b'\x89PNG') and response.headers['cache-control'] == 'no-store'
    assert client.get(f'/api/v1/documents/{identifier}/preview/2', headers=owner).status_code == 404
    assert client.get(f'/api/v1/documents/{identifier}/preview/1', headers=other).status_code == 404
    assert client.get(f'/api/v1/documents/{identifier}/preview/1').status_code == 401
    sheet = upload(client, owner).json()['document_id']
    assert client.get(f'/api/v1/documents/{sheet}/preview/1', headers=owner).status_code == 404
