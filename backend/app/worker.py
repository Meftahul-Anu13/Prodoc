"""Database-backed bounded worker. A killed process is recovered after its lease expires."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select, update

from . import search, textsearch
from .db import Audit, Document, Invoice, InvoiceVersion, Job, Page, database, now

MAX_ATTEMPTS = 3
LEASE_SECONDS = 900


def lease_seconds():
    # Slow local CPU models need longer leases; every worker must use the same value.
    try:
        value = int(os.getenv('WORKER_LEASE_SECONDS', str(LEASE_SECONDS)))
    except ValueError:
        return LEASE_SECONDS
    return value if 300 <= value <= 7200 else LEASE_SECONDS


def normalized(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').lower())


def transition(s, job, doc, status, error=None):
    previous = doc.status
    job.status = doc.status = status
    job.error_code = doc.error_code = error
    s.add(Audit(document_id=doc.id, action='job_transition', detail={'job_id': job.id, 'from': previous, 'to': status, 'attempt': job.attempts, 'error_code': error}))
    s.commit()


def claim(s):
    instant = now()
    candidate = s.scalar(select(Job).where(Job.status.in_(['queued', 'retrying']), Job.available_at <= instant).order_by(Job.available_at).limit(1))
    if not candidate:
        # Recover only expired leases, never steal work from an active process.
        expired = s.scalars(select(Job).where(Job.status.in_(['extracting', 'validating', 'reconciling']), Job.lease_until < instant)).all()
        for job in expired:
            doc = s.get(Document, job.document_id)
            if doc.extraction_metadata.get('requested_provider') in {'anthropic', 'gateway'}:
                # A provider may already have billed either observation or extraction.
                # An expired lease is not permission to submit either request again.
                transition(s, job, doc, 'failed', 'cloud_processing_outcome_unknown')
            elif job.attempts >= MAX_ATTEMPTS:
                transition(s, job, doc, 'failed', 'worker_lease_expired')
            else:
                job.available_at = instant
                transition(s, job, doc, 'retrying', 'worker_lease_expired')
        return None
    previous_status = candidate.status
    result = s.execute(update(Job).where(Job.id == candidate.id, Job.status == previous_status, Job.available_at <= instant)
                       .values(status='extracting', attempts=Job.attempts + 1, started_at=instant, lease_until=instant + timedelta(seconds=lease_seconds())).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        s.rollback()
        return None
    s.commit()
    s.refresh(candidate)
    return candidate


def add_duplicates(s, doc, invoices):
    existing = list(s.scalars(select(Invoice).join(Document).where(Document.owner_id == doc.owner_id, Invoice.active == 1, Invoice.document_id != doc.id)))
    for current in invoices:
        data = current.data
        links = []
        for other in existing:
            old = other.data
            supplier = normalized(data.get('supplier_name'))
            if not supplier or supplier != normalized(old.get('supplier_name')):
                continue
            number = normalized(data.get('invoice_number'))
            if number and number == normalized(old.get('invoice_number')):
                links.append({'invoice_id': other.id, 'rule_id': 'duplicate_supplier_number'})
            elif all(data.get(f) is not None and str(data[f]) == str(old.get(f)) for f in ('invoice_date', 'currency', 'total')):
                links.append({'invoice_id': other.id, 'rule_id': 'duplicate_supplier_date_total_candidate'})
        if links:
            data = dict(data)
            data['duplicates'] = links
            data['findings'] = list(data.get('findings', [])) + [{'rule_id': 'suspected_duplicate', 'severity': 'needs_review', 'message': 'Possible duplicate; review links, never auto-delete', 'rule_version': '1'}]
            data['needs_review'] = True
            current.data = data
        existing.append(current)


class ParserFailure(Exception):
    def __init__(self, code, permanent, artifact_run_id=None):
        self.code, self.permanent = code, permanent
        self.artifact_run_id = artifact_run_id
        super().__init__(code)


def isolated_parse(path, filename):
    from .artifacts import new_run
    # A child timeout is a crash/hang boundary, NOT an OS security sandbox.
    run_id, artifacts = new_run(path)
    try:
        timeout = int(os.getenv('PROCESS_TIMEOUT_SECONDS', '600'))
    except ValueError:
        raise ParserFailure('invalid_processing_timeout', True, run_id) from None
    if not 1 <= timeout < lease_seconds() - 30:
        raise ParserFailure('invalid_processing_timeout', True, run_id)
    with tempfile.TemporaryDirectory(prefix='docintel-parse-') as directory:
        destination = Path(directory) / 'result.json'
        try:
            subprocess.run([sys.executable, '-m', 'backend.app.parse_job', str(path), filename, str(destination), str(artifacts)],
                           cwd=Path(__file__).resolve().parents[2], check=True, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ParserFailure('processing_timeout_outcome_unknown', True, run_id)
        except subprocess.CalledProcessError:
            raise ParserFailure('parser_process_failed', True, run_id)
        try:
            result = json.loads(destination.read_text(encoding='utf-8'))
            if not result['ok']:
                raise ParserFailure(result['code'], bool(result.get('permanent')), run_id)
            processed = result['result']
            processed['metadata'] = {**processed.get('metadata', {}), 'artifact_run_id': run_id}
        except (OSError, ValueError, KeyError, TypeError):
            raise ParserFailure('parser_result_outcome_unknown', True, run_id) from None
        return processed


def execute_job(sessions, storage, job_id):
    with sessions() as s:
        job = s.get(Job, job_id)
        doc = s.get(Document, job.document_id)
        requested = os.getenv('EXTRACTION_PROVIDER', 'anthropic').lower()
        doc.extraction_metadata = {**doc.extraction_metadata, 'requested_provider': requested}
        transition(s, job, doc, 'extracting')
        try:
            result = isolated_parse((Path(storage) / doc.storage_key).resolve(), doc.filename)
            transition(s, job, doc, 'validating')
            metadata = result.get('metadata', {})
            if metadata.get('status') == 'failed':
                # Keep inspectable source failures instead of reporting empty extraction as success.
                for page in result.get('pages', []):
                    s.add(Page(document_id=doc.id, data=page))
                doc.page_count = len(result.get('pages', []))
                doc.extraction_metadata = metadata
                transition(s, job, doc, 'failed', 'source_extraction_failed')
                return
            s.execute(delete(Page).where(Page.document_id == doc.id))
            s.execute(update(Invoice).where(Invoice.document_id == doc.id).values(active=0))
            for page in result.get('pages', []):
                s.add(Page(document_id=doc.id, data=page))
            invoices = []
            for data in result.get('invoices', []):
                i = Invoice(document_id=doc.id, data=data, review_status='pending')
                s.add(i)
                s.flush()
                invoices.append(i)
            transition(s, job, doc, 'reconciling')
            add_duplicates(s, doc, invoices)
            for i in invoices:
                s.add(InvoiceVersion(invoice_id=i.id, version=1, data=i.data, reason='Initial extraction'))
            doc.page_count = len(result.get('pages', []))
            doc.search_text = '\n'.join(str(p.get('text', '')) for p in result.get('pages', []))
            doc.search_text += '\n' + '\n'.join(' '.join(str(i.data.get(f) or '') for f in ('supplier_name', 'invoice_number', 'invoice_date', 'total', 'currency')) for i in invoices)
            doc.extraction_metadata = {**metadata, 'requested_provider': requested, 'index_status': 'pending' if search.enabled() else 'disabled'}
            textsearch.upsert(s, doc)
            job.finished_at = now()
            job.lease_until = None
            # A total passing arithmetic is not human approval. All baseline outputs await review.
            transition(s, job, doc, 'needs_review')
        except Exception as exc:
            s.rollback()
            job, doc = s.get(Job, job_id), s.get(Document, doc.id)
            from .pipeline import PipelineError
            permanent = exc.permanent if isinstance(exc, ParserFailure) else isinstance(exc, (PipelineError, ValueError, FileNotFoundError))
            code = exc.code if isinstance(exc, ParserFailure) else ('invalid_document' if permanent else 'processing_error')
            if requested in {'anthropic', 'gateway'} and not isinstance(exc, ParserFailure):
                permanent, code = True, 'cloud_processing_outcome_unknown'
            if isinstance(exc, ParserFailure) and exc.artifact_run_id:
                doc.extraction_metadata = {**doc.extraction_metadata, 'artifact_run_id': exc.artifact_run_id}
            job.lease_until = None
            if not permanent and job.attempts < MAX_ATTEMPTS:
                job.available_at = now() + timedelta(seconds=2 ** job.attempts)
                transition(s, job, doc, 'retrying', code)
            else:
                job.finished_at = now()
                transition(s, job, doc, 'failed', code)


def index_pending(sessions):
    if not search.enabled():
        return
    # A missing collection (deleted/lost index folder) means every document must be rebuilt.
    rebuild = not search.collection_ready()
    with sessions() as s:
        docs = list(s.scalars(select(Document).where(Document.status.in_(['needs_review', 'completed']))))
        for doc in docs:
            meta = dict(doc.extraction_metadata)
            # 'disabled' documents were processed while search was off; documents
            # indexed by an older chunking/embedding version are rebuilt.
            stale = meta.get('index_status') == 'indexed' and (rebuild or meta.get('index_version') != search.INDEX_VERSION)
            if meta.get('index_status') not in {'pending', 'retrying', 'disabled'} and not stale:
                continue
            if stale:
                meta['index_attempts'] = 0
            attempts = int(meta.get('index_attempts', 0))
            if attempts >= 3:
                continue
            try:
                search.index_document(doc.id, doc.owner_id, doc.search_text)
                meta['index_status'] = 'indexed'
                meta['index_version'] = search.INDEX_VERSION
            except Exception:
                meta['index_status'] = 'retrying' if attempts < 2 else 'failed'
            meta['index_attempts'] = attempts + 1
            doc.extraction_metadata = meta
            s.commit()


def tick(sessions, storage):
    with sessions() as s:
        job = claim(s)
        job_id = job.id if job else None
    if job_id:
        execute_job(sessions, storage, job_id)
    return bool(job_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Process available jobs then exit')
    args = parser.parse_args()
    engine, sessions = database()
    storage = os.getenv('STORAGE_DIR', './var/originals')
    while True:
        worked = tick(sessions, storage)
        if not worked:
            if search.indexer() == 'worker':
                index_pending(sessions)
            if args.once:
                break
            time.sleep(1)
    engine.dispose()


if __name__ == '__main__':
    main()
