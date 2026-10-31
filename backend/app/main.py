"""Authenticated document API. Run the durable worker separately: python -m backend.app.worker."""
import copy
import hashlib
import logging
import os
import threading
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import jwt
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from . import preview, search, textsearch
from .db import Audit, Document, Invoice, InvoiceVersion, Job, Page, Reconciliation, User, database, now, uid
from .security import RateLimiter, signing_key, token, verify_password

ALLOWED = {'.pdf', '.jpg', '.png', '.tiff', '.xlsx', '.xls', '.csv'}
EDITABLE = {'invoice_number', 'invoice_date', 'supplier_name', 'customer_name', 'subtotal', 'tax', 'discount', 'other_charges', 'total', 'currency', 'line_amount_basis', 'line_items'}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Credentials(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class Correction(StrictModel):
    version: int = Field(ge=1)
    changes: dict = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=1000)


class Version(StrictModel):
    version: int = Field(ge=1)


class Pair(StrictModel):
    left_invoice_id: str
    right_invoice_id: str


class Query(StrictModel):
    query: str = Field(min_length=1, max_length=1000)


class SmartQuery(StrictModel):
    query: str = Field(min_length=1, max_length=1000)
    modes: list[Literal['keyword', 'semantic']] = Field(default=['keyword', 'semantic'], min_length=1)


def doc_json(d):
    return {'id': d.id, 'filename': d.filename, 'status': d.status, 'created_at': d.created_at.isoformat(),
            'page_count': d.page_count, 'error_code': d.error_code, 'metadata': d.extraction_metadata}


def inv_json(i):
    return {'id': i.id, 'document_id': i.document_id, 'version': i.version, 'data': i.data, 'review_status': i.review_status, 'active': bool(i.active)}


def create_app(database_url=None, storage_dir=None, jwt_secret=None):
    engine, sessions = database(database_url)
    key = jwt_secret or signing_key()
    storage = Path(storage_dir or os.getenv('STORAGE_DIR', './var/originals')).resolve()
    storage.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter()

    @asynccontextmanager
    async def lifespan(app):
        stop = threading.Event()
        if search.enabled() and search.indexer() == 'api':
            # Embedded Qdrant allows one process: index pending documents here.
            from .worker import index_pending

            def loop():
                while not stop.wait(10):
                    try:
                        index_pending(sessions)
                    except Exception:
                        pass  # index_pending records per-document failures itself
            threading.Thread(target=loop, daemon=True, name='semantic-indexer').start()
        yield
        stop.set()
        engine.dispose()

    app = FastAPI(title='Document Intelligence', version='1.0.0', lifespan=lifespan)
    app.state.sessions = sessions
    app.state.storage = storage

    @app.middleware('http')
    async def request_id(request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(HTTPException)
    async def safe_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={'error': {'code': str(exc.detail), 'message': str(exc.detail).replace('_', ' ')}, 'request_id': request.state.request_id})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={'error': {'code': 'invalid_request', 'message': 'Request does not match the API schema'}, 'request_id': request.state.request_id})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse(status_code=500, content={'error': {'code': 'internal_error', 'message': 'Request failed; use the request ID for diagnosis'}, 'request_id': request.state.request_id})

    def session():
        with sessions() as s:
            yield s

    def actor(authorization: Annotated[str | None, Header()] = None, s=Depends(session)):
        if not authorization or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'authentication_required')
        try:
            payload = jwt.decode(authorization[7:], key, algorithms=['HS256'], options={'require': ['sub', 'exp', 'iat', 'jti']})
        except jwt.PyJWTError:
            raise HTTPException(401, 'invalid_token')
        user = s.get(User, payload['sub'])
        if not user:
            raise HTTPException(401, 'invalid_token')
        return user

    def document(s, identifier, user):
        d = s.get(Document, identifier)
        if not d or (d.owner_id != user.id and user.role != 'admin'):
            raise HTTPException(404, 'document_not_found')
        return d

    def invoice(s, identifier, user):
        i = s.get(Invoice, identifier)
        if not i:
            raise HTTPException(404, 'invoice_not_found')
        document(s, i.document_id, user)
        if not i.active:
            raise HTTPException(409, 'superseded_invoice')
        return i

    def reviewer(user):
        if user.role not in {'reviewer', 'admin'}:
            raise HTTPException(403, 'reviewer_required')

    @app.post('/api/v1/auth/login')
    def login(body: Credentials, request: Request, s=Depends(session)):
        limiter.check(('login', request.client.host), 10)
        user = s.scalar(select(User).where(User.email == body.email.lower()))
        # The dummy verification avoids a cheap unknown-email timing branch.
        if not user:
            from .security import hasher
            verify_password(hasher.hash('non-user-dummy-password'), body.password)
            raise HTTPException(401, 'invalid_credentials')
        if not verify_password(user.password_hash, body.password):
            raise HTTPException(401, 'invalid_credentials')
        return {'access_token': token(user, key), 'token_type': 'bearer', 'user': {'id': user.id, 'email': user.email, 'role': user.role}}

    @app.get('/api/v1/auth/me')
    def me(user=Depends(actor)):
        return {'id': user.id, 'email': user.email, 'role': user.role}

    async def ingest(file, idem, user, s):
        filename = Path(file.filename or '').name.replace('\\', '_')[:255]
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED:
            raise HTTPException(415, 'unsupported_format')
        identifier = uid()
        storage_key = identifier + suffix
        path = storage / storage_key
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open('xb') as out:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > int(os.getenv('MAX_UPLOAD_BYTES', '104857600')):
                        raise HTTPException(413, 'file_too_large')
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise HTTPException(422, 'empty_file')
            header = path.read_bytes()[:16] if size < 16 else None
            if header is None:
                with path.open('rb') as src:
                    header = src.read(16)
            signatures = {'.pdf': [b'%PDF-'], '.jpg': [b'\xff\xd8\xff'], '.png': [b'\x89PNG\r\n\x1a\n'], '.tiff': [b'II*\x00', b'MM\x00*'], '.xlsx': [b'PK\x03\x04'], '.xls': [b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1']}
            if suffix in signatures and not any(header.startswith(magic) for magic in signatures[suffix]):
                raise HTTPException(422, 'file_signature_mismatch')
            if suffix == '.csv' and b'\x00' in header and not header.startswith((b'\xff\xfe', b'\xfe\xff')):
                raise HTTPException(422, 'binary_csv')
            existing = s.scalar(select(Document).where(Document.owner_id == user.id, Document.sha256 == digest.hexdigest()))
            if idem:
                keyed = s.scalar(select(Document).where(Document.owner_id == user.id, Document.idempotency_key == idem))
                if keyed and keyed.sha256 != digest.hexdigest():
                    raise HTTPException(409, 'idempotency_key_conflict')
                existing = keyed or existing
            if existing:
                path.unlink()
                job = s.scalar(select(Job).where(Job.document_id == existing.id).order_by(Job.available_at.desc()))
                return {'document_id': existing.id, 'job_id': job.id, 'status_url': f'/api/v1/documents/{existing.id}/status', 'deduplicated': True}
            d = Document(id=identifier, owner_id=user.id, filename=filename, sha256=digest.hexdigest(), idempotency_key=idem, storage_key=storage_key, status='queued')
            j = Job(id=uid(), document_id=identifier)
            s.add(d)
            s.flush()
            s.add(j)
            s.add(Audit(document_id=identifier, actor_id=user.id, action='uploaded', detail={'job_id': j.id, 'bytes': size}))
            s.commit()
            return {'document_id': identifier, 'job_id': j.id, 'status_url': f'/api/v1/documents/{identifier}/status'}
        except IntegrityError:
            s.rollback()
            path.unlink(missing_ok=True)
            raise HTTPException(409, 'concurrent_duplicate_upload_retry')
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @app.post('/api/v1/documents', status_code=202)
    async def upload(file: UploadFile = File(...), idempotency_key: Annotated[str | None, Header(max_length=128)] = None, user=Depends(actor), s=Depends(session)):
        limiter.check(('upload', user.id), 30)
        return await ingest(file, idempotency_key, user, s)

    @app.post('/api/v1/documents/batch', status_code=202)
    async def batch(files: list[UploadFile] = File(...), user=Depends(actor), s=Depends(session)):
        limiter.check(('upload', user.id), 30)
        if len(files) > 50:
            raise HTTPException(422, 'batch_limit_exceeded')
        results = []
        for file in files:
            name = file.filename
            try:
                results.append({'filename': name, **await ingest(file, None, user, s)})
            except HTTPException as exc:
                s.rollback()
                results.append({'filename': name, 'error': exc.detail})
        return {'items': results}

    @app.get('/api/v1/documents')
    @app.get('/api/v1/search')
    def documents(q: str = '', status: str | None = None, cursor: str | None = None, limit: int = 50, user=Depends(actor), s=Depends(session)):
        limiter.check(('search', user.id), 120)
        if len(q) > 1000 or not 1 <= limit <= 100:
            raise HTTPException(422, 'invalid_search')
        query = select(Document)
        if user.role != 'admin':
            query = query.where(Document.owner_id == user.id)
        if q:
            query = query.where(Document.search_text.ilike('%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%', escape='\\'))
        if status:
            query = query.where(Document.status == status)
        if cursor:
            query = query.where(Document.id > cursor)
        rows = s.scalars(query.order_by(Document.id).limit(limit + 1)).all()
        return {'items': [doc_json(d) for d in rows[:limit]], 'next_cursor': rows[limit - 1].id if len(rows) > limit else None}

    @app.post('/api/v1/search/semantic')
    def semantic(body: Query, user=Depends(actor), s=Depends(session)):
        limiter.check(('search', user.id), 30)
        try:
            semantic_ranking = search.semantic_ids(body.query, user.id, user.role == 'admin')
        except Exception:
            fallback = documents(body.query, None, None, 50, user, s)
            return {**fallback, 'mode': 'keyword_fallback', 'warning': 'semantic_index_unavailable'}
        keyword = documents(body.query, None, None, 50, user, s)['items']
        ids = search.rrf([d['id'] for d in keyword], semantic_ranking)
        output = []
        for identifier in ids:
            d = s.get(Document, identifier)
            # Re-authorize every vector hit. Payload filters are not the security boundary.
            if d and (d.owner_id == user.id or user.role == 'admin'):
                output.append(doc_json(d))
        return {'items': output, 'next_cursor': None, 'mode': 'hybrid_rrf'}

    @app.post('/api/v1/search/smart')
    def smart(body: SmartQuery, user=Depends(actor), s=Depends(session)):
        """Exact + typo-tolerant keyword + semantic results, each labeled with why it matched."""
        limiter.check(('search', user.id), 60)
        admin = user.role == 'admin'
        keyword = textsearch.keyword_hits(s, body.query, user.id, admin) if 'keyword' in body.modes else []
        s.commit()  # persist any backfilled index rows
        semantic, warning = [], None
        # Meaning search cannot match identifiers or amounts; in combined mode it only adds noise for them.
        words = textsearch.query_words(body.query)
        identifier_only = bool(words) and all(any(c.isdigit() for c in w) for w in words)
        if 'semantic' in body.modes and not (identifier_only and 'keyword' in body.modes):
            try:
                semantic = search.semantic_hits(body.query, user.id, admin)
            except Exception as exc:
                # Type only: messages from index libraries could echo stored data.
                logging.getLogger(__name__).warning('semantic search unavailable: %s', type(exc).__name__)
                warning = 'semantic_index_unavailable'
        matches = {}
        for hit in keyword:
            matches.setdefault(hit['document_id'], []).append(hit)
        for hit in semantic:
            matches.setdefault(hit['document_id'], [])
        order = search.rrf([h['document_id'] for h in keyword], [h['document_id'] for h in semantic])
        tier = {h['document_id']: (0 if h['type'] == 'exact' else 1) for h in keyword}
        order.sort(key=lambda identifier: tier.get(identifier, 2))  # stable: exact, fuzzy, then meaning-only
        semantic_by_id = {h['document_id']: h for h in semantic}
        output = []
        for identifier in order:
            d = s.get(Document, identifier)
            # Re-authorize every hit against SQL; index payloads are not the security boundary.
            if not d or (d.owner_id != user.id and not admin):
                continue
            found = list(matches.get(identifier, []))
            if identifier in semantic_by_id:
                hit = semantic_by_id[identifier]
                found.append({'document_id': identifier, 'type': 'semantic', 'score': hit['score'],
                              'snippet': textsearch.clean_line(search.chunk_at(d.search_text, hit['offset'])), 'highlights': []})
            output.append({**doc_json(d), 'matches': [{k: v for k, v in m.items() if k != 'document_id'} for m in found]})
        return {'items': output, 'modes': body.modes, **({'warning': warning} if warning else {})}

    @app.get('/api/v1/documents/{identifier}')
    def get_document(identifier: str, user=Depends(actor), s=Depends(session)):
        return doc_json(document(s, identifier, user))

    @app.get('/api/v1/documents/{identifier}/status')
    def status(identifier: str, user=Depends(actor), s=Depends(session)):
        d = document(s, identifier, user)
        j = s.scalar(select(Job).where(Job.document_id == identifier).order_by(Job.available_at.desc()))
        return {**doc_json(d), 'job_id': j.id if j else None, 'attempts': j.attempts if j else 0}

    @app.get('/api/v1/documents/{identifier}/results')
    def results(identifier: str, user=Depends(actor), s=Depends(session)):
        d = document(s, identifier, user)
        return {'document': doc_json(d), 'invoices': [inv_json(i) for i in s.scalars(select(Invoice).where(Invoice.document_id == identifier, Invoice.active == 1))],
                'pages': [p.data for p in s.scalars(select(Page).where(Page.document_id == identifier))],
                'events': [{'action': a.action, 'created_at': a.created_at.isoformat(), 'detail': a.detail} for a in s.scalars(select(Audit).where(Audit.document_id == identifier).order_by(Audit.created_at))]}

    @app.get('/api/v1/documents/{identifier}/artifacts/{name}')
    def get_artifact(identifier: str, name: str, user=Depends(actor), s=Depends(session)):
        from .artifacts import artifact_path
        d = document(s, identifier, user)
        try:
            path = artifact_path(storage, d.storage_key, d.extraction_metadata.get('artifact_run_id'), name)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, 'artifact_not_found')
        media = 'text/markdown; charset=utf-8' if name.endswith('.md') else 'application/json'
        return FileResponse(path, media_type=media, filename=name)

    @app.get('/api/v1/documents/{identifier}/preview/{page}')
    def page_preview(identifier: str, page: int, user=Depends(actor), s=Depends(session)):
        d = document(s, identifier, user)
        if not preview.supported(d.filename):
            raise HTTPException(404, 'preview_not_available')
        try:
            image = preview.render(storage / d.storage_key, d.filename, page)
        except HTTPException:
            raise
        except Exception:  # corrupt originals and out-of-range pages expose no parser detail
            raise HTTPException(404, 'preview_not_available')
        return Response(image, media_type='image/png')

    @app.get('/api/v1/documents/{identifier}/audit')
    def history(identifier: str, user=Depends(actor), s=Depends(session)):
        document(s, identifier, user)
        audits = s.scalars(select(Audit).where(Audit.document_id == identifier).order_by(Audit.created_at)).all()
        versions = s.scalars(select(InvoiceVersion).join(Invoice).where(Invoice.document_id == identifier)).all()
        return {'items': [{'id': a.id, 'action': a.action, 'actor_id': a.actor_id, 'detail': a.detail, 'created_at': a.created_at.isoformat()} for a in audits],
                'versions': [{'invoice_id': v.invoice_id, 'version': v.version, 'data': v.data, 'reason': v.reason} for v in versions]}

    @app.post('/api/v1/documents/{identifier}/reprocess', status_code=202)
    def reprocess(identifier: str, user=Depends(actor), s=Depends(session)):
        d = document(s, identifier, user)
        if d.status not in {'failed', 'needs_review', 'completed'}:
            raise HTTPException(409, 'job_in_progress')
        previous = d.status
        d.status, d.error_code = 'queued', None
        j = Job(id=uid(), document_id=d.id)
        s.add(j)
        s.add(Audit(document_id=d.id, actor_id=user.id, action='reprocess', detail={'from': previous, 'to': 'queued', 'job_id': j.id}))
        s.commit()
        return {'document_id': d.id, 'job_id': j.id, 'status_url': f'/api/v1/documents/{d.id}/status'}

    @app.post('/api/v1/invoices/{identifier}/corrections')
    def correct(identifier: str, body: Correction, user=Depends(actor), s=Depends(session)):
        from .pipeline import validate_invoice
        reviewer(user)
        i = invoice(s, identifier, user)
        if i.version != body.version:
            raise HTTPException(409, 'version_conflict')
        if set(body.changes) - EDITABLE:
            raise HTTPException(422, 'unsupported_correction_field')
        data = copy.deepcopy(i.data)
        for field, value in body.changes.items():
            if field == 'line_items':
                if not isinstance(value, list) or len(value) > 10000 or not all(isinstance(row, dict) for row in value):
                    raise HTTPException(422, 'invalid_line_items')
            elif value is not None and (not isinstance(value, str) or len(value) > 1000):
                raise HTTPException(422, 'corrections_require_strings_or_null')
            data[field] = value
            data.setdefault('evidence', {})[field] = {'method': 'human_correction', 'actor_id': user.id, 'reason': body.reason, 'original_evidence': i.data.get('evidence', {}).get(field)}
        try:
            findings = validate_invoice(data)
        except (ValueError, TypeError, ArithmeticError):
            raise HTTPException(422, 'invalid_corrected_value')
        data['findings'], data['needs_review'] = findings, True
        next_version = i.version + 1
        updated = s.execute(update(Invoice).where(Invoice.id == i.id, Invoice.version == body.version).values(data=data, version=next_version, review_status='pending'))
        if updated.rowcount != 1:
            s.rollback()
            raise HTTPException(409, 'version_conflict')
        s.add(InvoiceVersion(invoice_id=i.id, version=next_version, data=data, actor_id=user.id, reason=body.reason))
        s.add(Audit(document_id=i.document_id, actor_id=user.id, action='corrected', detail={'invoice_id': i.id, 'version': next_version, 'fields': sorted(body.changes)}))
        d = s.get(Document, i.document_id)
        d.status = 'needs_review'
        # Corrected values are immediately keyword searchable; old evidence remains available.
        d.search_text += '\n' + ' '.join(str(v) for v in body.changes.values() if v is not None)
        s.commit()
        s.expire_all()
        return inv_json(s.get(Invoice, identifier))

    @app.post('/api/v1/invoices/{identifier}/finalize')
    def finalize(identifier: str, body: Version, user=Depends(actor), s=Depends(session)):
        reviewer(user)
        i = invoice(s, identifier, user)
        if i.version != body.version:
            raise HTTPException(409, 'version_conflict')
        d = s.get(Document, i.document_id)
        if d.status not in {'needs_review', 'completed'}:
            raise HTTPException(409, 'document_not_ready_for_review')
        if d.extraction_metadata.get('finalization_blocked'):
            raise HTTPException(409, 'incomplete_source_coverage_requires_reprocess')
        if any(f.get('severity') in {'invalid', 'error'} for f in i.data.get('findings', [])):
            raise HTTPException(409, 'invalid_invoice_requires_correction')
        updated = s.execute(update(Invoice).where(Invoice.id == identifier, Invoice.version == body.version).values(review_status='approved', version=body.version + 1))
        if updated.rowcount != 1:
            raise HTTPException(409, 'version_conflict')
        s.add(InvoiceVersion(invoice_id=i.id, version=body.version + 1, data=i.data, actor_id=user.id, reason='Reviewer approval'))
        s.add(Audit(document_id=i.document_id, actor_id=user.id, action='approved', detail={'invoice_id': i.id, 'version': body.version + 1}))
        s.flush()
        remaining = s.scalar(select(Invoice.id).where(Invoice.document_id == i.document_id, Invoice.active == 1, Invoice.review_status != 'approved').limit(1))
        if remaining is None:
            s.get(Document, i.document_id).status = 'completed'
        s.commit()
        s.expire_all()
        return inv_json(s.get(Invoice, identifier))

    @app.post('/api/v1/reconciliations/run')
    def run_reconciliation(body: Pair, user=Depends(actor), s=Depends(session)):
        from .pipeline import reconcile
        left, right = invoice(s, body.left_invoice_id, user), invoice(s, body.right_invoice_id, user)
        if left.id == right.id:
            raise HTTPException(422, 'distinct_records_required')
        result = reconcile(left.data, right.data)
        r = Reconciliation(id=uid(), owner_id=user.id, left_invoice_id=left.id, right_invoice_id=right.id, left_version=left.version, right_version=right.version, data=result)
        s.add(r)
        s.add(Audit(document_id=left.document_id, actor_id=user.id, action='reconciled', detail={'reconciliation_id': r.id, 'right_document_id': right.document_id}))
        s.commit()
        return {'id': r.id, **result, 'left_version': r.left_version, 'right_version': r.right_version}

    @app.get('/api/v1/reconciliations')
    def reconciliations(user=Depends(actor), s=Depends(session)):
        query = select(Reconciliation)
        if user.role != 'admin':
            query = query.where(Reconciliation.owner_id == user.id)
        return {'items': [{'id': r.id, **r.data, 'left_invoice_id': r.left_invoice_id, 'right_invoice_id': r.right_invoice_id, 'left_version': r.left_version, 'right_version': r.right_version} for r in s.scalars(query.limit(100))]}

    @app.delete('/api/v1/documents/{identifier}')
    def remove(identifier: str, user=Depends(actor), s=Depends(session)):
        d = document(s, identifier, user)
        if d.status not in {'failed', 'needs_review', 'completed'}:
            raise HTTPException(409, 'job_in_progress')
        try:
            search.delete_document(d.id)
        except Exception:
            raise HTTPException(503, 'index_delete_failed_retry')
        original = storage / d.storage_key
        textsearch.remove(s, d.id)
        s.add(Audit(document_id=d.id, actor_id=user.id, action='deleted', detail={'policy': 'retain_redacted_audit'}))
        s.execute(delete(Document).where(Document.id == d.id))
        s.commit()
        original.unlink(missing_ok=True)
        from .artifacts import document_directory
        artifacts = document_directory(storage, d.storage_key)
        if artifacts.is_dir() and not artifacts.is_symlink():
            shutil.rmtree(artifacts)
        return {'deleted': True}

    @app.post('/api/v1/admin/reindex', status_code=202)
    def reindex(user=Depends(actor), s=Depends(session)):
        if user.role != 'admin':
            raise HTTPException(403, 'admin_required')
        if not search.enabled():
            raise HTTPException(503, 'semantic_disabled')
        count = 0
        for d in s.scalars(select(Document).where(Document.status.in_(['completed', 'needs_review']))):
            d.extraction_metadata = {**d.extraction_metadata, 'index_status': 'pending'}
            count += 1
        s.commit()
        return {'queued': count}

    @app.get('/api/v1/healthz')
    @app.get('/healthz')
    def health():
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text('SELECT 1'))
        return {'status': 'ok', 'semantic_enabled': search.enabled()}

    return app
