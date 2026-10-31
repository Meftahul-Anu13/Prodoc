"""Isolated parser/observation entry point; artifacts remain private and versioned."""
import hashlib
import json
import os
import sys
from pathlib import Path

from .pipeline import PipelineError, process_file


class ProcessingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _write(directory, name, value):
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value, encoding='utf-8')
    else:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=1, allow_nan=False), encoding='utf-8')


def _model_provider(provider):
    if provider == 'local':
        from .local_llm import OllamaObservationProvider
        return OllamaObservationProvider()
    if provider == 'gateway':
        # Configured once in gateway_settings.py; the operator chose this endpoint.
        from .gateway_llm import GatewayObservationProvider
        return GatewayObservationProvider()
    return None  # observe_extract builds the consent-checked Anthropic provider


def _cache_key(provider, model, bundle):
    from .observation import PROMPT_VERSION, SCHEMA_VERSION
    material = {'blocks': bundle['blocks'], 'provider': provider, 'model': model, 'prompt': PROMPT_VERSION,
                'schema': SCHEMA_VERSION, 'decimal_style': os.getenv('MONEY_DECIMAL_STYLE', 'dot')}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _cache_dir():
    if os.getenv('EXTRACTION_CACHE', 'on').lower() == 'off':
        return None
    # Results contain document data: keep the cache inside private storage.
    root = os.getenv('EXTRACTION_CACHE_DIR') or str(Path(os.getenv('STORAGE_DIR', './var/originals')) / '.cache' / 'extraction')
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _extract(bundle, directory, provider, model_provider):
    """One observe->extract run, reused from the content-hash cache when identical input was done before."""
    from .observation import observe_extract
    model = getattr(model_provider, 'model', None) or os.getenv('EXTRACTION_MODEL') or os.getenv('LLM_MODEL')
    cache = _cache_dir()
    key = _cache_key(provider, model, bundle) if cache else None
    if cache and (cache / f'{key}.json').is_file():
        result = json.loads((cache / f'{key}.json').read_text(encoding='utf-8'))
        result['metadata'] = {**result['metadata'], 'cache': 'hit'}
        return result
    result = observe_extract(bundle, Path(directory)) if model_provider is None else observe_extract(bundle, Path(directory), model_provider)
    if cache:
        temporary = cache / f'{key}.{os.getpid()}.tmp'
        temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        os.replace(temporary, cache / f'{key}.json')
    return result


def _merge(results, requests, plan):
    """Combine per-request results into one document result (IDs prefixed by request)."""
    invoices, runs, reasons, warnings, observation = [], [], set(), [], {'layout': f"{len(plan['segments'])} invoice segments in {len(results)} requests", 'regions': [], 'groups': [], 'ignored_source_ids': [], 'warnings': []}
    coverage = {}
    for index, (request, result) in enumerate(zip(requests, results), 1):
        meta = result['metadata']
        prefix = f'b{index}:'
        for invoice in result['invoices']:
            invoices.append({**invoice, 'group_id': prefix + str(invoice.get('group_id'))})
        runs.extend({**run, 'request': index} for run in meta.get('extraction_runs', []))
        reasons.update(meta.get('finalization_blocked_reasons', []))
        warnings.extend(f'request {index} (pages {request["pages"][0]}-{request["pages"][-1]}): {w}' for w in meta.get('warnings', []))
        seen = meta.get('observation') or {}
        observation['regions'].extend({**r, 'id': prefix + r['id']} for r in seen.get('regions', []))
        observation['groups'].extend({**g, 'id': prefix + g['id'], 'region_ids': [prefix + r for r in g['region_ids']]} for g in seen.get('groups', []))
        observation['ignored_source_ids'].extend(seen.get('ignored_source_ids', []))
        for key, value in (meta.get('coverage') or {}).items():
            if isinstance(value, bool):
                coverage[key] = coverage.get(key, True) and value
            elif isinstance(value, (int, float)):
                coverage[key] = coverage.get(key, 0) + value
            elif isinstance(value, list):
                coverage[key] = coverage.get(key, []) + value
        request['invoices'] = len(result['invoices'])
        request['cache'] = meta.get('cache', 'miss')
        if any(plan['segments'][i].get('cut_by_size') for i in request['segments']):
            reasons.add('invoice_split_by_size_limit')
    first = results[0]['metadata']
    return invoices, {**first, 'extraction_runs': runs, 'observation': observation, 'coverage': coverage,
                      'warnings': warnings, 'finalization_blocked': bool(reasons),
                      'finalization_blocked_reasons': sorted(reasons),
                      'status': 'partial' if reasons else 'processed'}


def process_document(source, filename, artifact_dir=None):
    provider = os.getenv('EXTRACTION_PROVIDER', 'anthropic').lower()
    if provider == 'baseline':
        # Explicit offline comparison only, never a silent fallback from AI failure.
        return process_file(Path(source), filename)
    if provider not in {'anthropic', 'local', 'gateway'}:
        raise ProcessingError('unsupported_extraction_provider')
    # 'local' runs an open-weight model on this machine: no transmission or billing.
    if provider == 'anthropic' and os.getenv('ALLOW_CLOUD_EXTRACTION', 'false').lower() != 'true':
        raise ProcessingError('cloud_extraction_requires_explicit_opt_in')
    if artifact_dir is None:
        from .artifacts import new_run
        _, artifact_dir = new_run(source)
    from .sources import read_sources
    from . import docmarkdown, guardrails, segmentation
    directory = Path(artifact_dir)
    bundle = read_sources(Path(source), filename, directory)
    try:
        _write(directory, 'document.md', docmarkdown.to_markdown(bundle, filename))
    except Exception:
        pass  # A derived view must never fail processing.
    mode = guardrails.mode()
    report = guardrails.assess(bundle) if mode != 'off' else None
    flags = []
    if report:
        _write(directory, 'guardrail.json', report)
        if report['verdict'] == 'not_financial':
            if mode == 'enforce':
                # Stop before any model call: no cost, no fabricated "invoice".
                raise ProcessingError('not_a_financial_document')
            flags.append('not_a_financial_document')
        elif report['verdict'] == 'uncertain':
            flags.append('low_financial_signal')
        if report['instruction_like_text']:
            flags.append('instruction_like_text_detected')
    plan = segmentation.plan(bundle, report)
    _write(directory, 'segments.json', plan)
    model_provider = _model_provider(provider)
    if plan['mode'] == 'single':
        result = _extract(bundle, directory, provider, model_provider)
        invoices, metadata = result['invoices'], result['metadata']
    else:
        from concurrent.futures import ThreadPoolExecutor
        requests = segmentation.batches(bundle, plan)
        plan['requests'] = requests

        def run(item):
            index, request = item
            request_dir = directory / f'request-{index:04d}'
            request_dir.mkdir(exist_ok=True)
            return _extract(segmentation.sub_bundle(bundle, request['pages']), request_dir, provider, model_provider)
        # A few concurrent requests for remote providers; local CPU models run one at a time.
        try:
            workers = max(1, min(8, int(os.getenv('EXTRACTION_CONCURRENCY', '1' if provider == 'local' else '3'))))
        except ValueError:
            workers = 1
        # Each finished request is cached, so a failed or timed-out document
        # resumes from the remaining requests when it is reprocessed.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run, enumerate(requests, 1)))
        invoices, metadata = _merge(results, requests, plan)
        _write(directory, 'segments.json', plan)
        _write(directory, 'grounded_invoices.json', {'invoices': invoices})
    guard = {'verdict': report['verdict'], 'relevant_pages': len(report['relevant_pages']),
             'skipped_pages': plan['skipped_pages'], 'instruction_like_text': report['instruction_like_text'][:20]} if report else None
    warnings = list(metadata.get('warnings', []))
    if 'instruction_like_text_detected' in flags:
        warnings.append('Document contains instruction-like text aimed at AI systems; it was treated as data. Verify values carefully.')
    if plan['skipped_pages']:
        warnings.append(f"{len(plan['skipped_pages'])} page(s) without financial content were not sent for extraction; see guardrail.json.")
    return {'pages': bundle['pages'], 'invoices': invoices,
        'metadata': {**bundle['metadata'], **metadata, 'filename': filename, 'warnings': warnings,
                     'guardrail': guard, 'review_flags': flags,
                     'segmentation': {'mode': plan['mode'], 'segments': len(plan['segments']),
                                      'requests': len(plan.get('requests', [])) or 1, 'skipped_pages': plan['skipped_pages']},
                     'requested_provider': provider, 'status': 'processed', 'needs_review': True}}


def main():
    source, filename, destination = sys.argv[1:4]
    artifact_dir = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    try:
        processed = process_document(source, filename, artifact_dir)
        result = {'ok': True, 'result': processed}
    except (PipelineError, ValueError, FileNotFoundError) as exc:
        # Source/observation errors expose stable codes, not upstream document text.
        code = getattr(exc, 'code', 'invalid_document')
        result = {'ok': False, 'code': code, 'permanent': True}
    except Exception:
        result = {'ok': False, 'code': 'processing_outcome_unknown', 'permanent': True}
    Path(destination).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
