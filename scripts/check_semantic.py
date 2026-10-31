"""Real ONNX + embedded Qdrant contract check; no API server or Docker required."""
import json
import os
import tempfile
from pathlib import Path

from qdrant_client import QdrantClient

from backend.app import search


def main():
    os.environ['SEMANTIC_ENABLED'] = 'true'
    with tempfile.TemporaryDirectory(prefix='docintel-vectors-') as directory:
        q = QdrantClient(path=directory)
        search.client = lambda: q
        search.index_document('doc-a', 'owner-a', 'Invoice INV-100 for laptop computers and office electronics. Total USD 110.00.')
        search.index_document('doc-b', 'owner-b', 'Invoice SECRET-B for hospital surgery and medical services. Total USD 900.00.')
        ids = search.semantic_ids('portable computer equipment', 'owner-a')
        assert 'doc-a' in ids
        assert 'doc-b' not in ids
        other = search.semantic_ids('medical services', 'owner-b')
        assert other == ['doc-b']
        search.delete_document('doc-a')
        assert search.semantic_ids('laptops', 'owner-a') == []
        q.close()
    output = {'model': 'BAAI/bge-small-en-v1.5', 'backend': 'Qdrant embedded', 'real_embedding_inference': True,
              'checks': ['index and query', 'owner payload isolation', 'delete'], 'passed': True,
              'limitation': 'Embedded contract check, not deployed Qdrant network integration or retrieval accuracy benchmark'}
    Path('eval/reports').mkdir(parents=True, exist_ok=True)
    Path('eval/reports/semantic-check.json').write_text(json.dumps(output, indent=2), encoding='utf-8')
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
