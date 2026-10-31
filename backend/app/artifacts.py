"""Private, versioned processing artifacts; never use a client-supplied disk path."""
import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4


ARTIFACT_NAMES = frozenset({'source.json', 'source_error.json', 'observation_bundle.json',
    'observation.json', 'extraction.json', 'observation_metadata.json', 'grounded_invoices.json',
    'guardrail.json', 'segments.json', 'document.md'})


def document_directory(storage, storage_key):
    root = Path(os.getenv('ARTIFACTS_DIR', str(Path(storage) / '.artifacts'))).resolve()
    key = hashlib.sha256(storage_key.encode('utf-8')).hexdigest()
    return root / key


def new_run(source):
    source = Path(source).resolve()
    identifier = uuid4().hex
    directory = document_directory(source.parent, source.name) / identifier
    directory.mkdir(parents=True, exist_ok=False)
    return identifier, directory


def artifact_path(storage, storage_key, run_id, name):
    if not re.fullmatch(r'[0-9a-f]{32}', str(run_id)) or name not in ARTIFACT_NAMES:
        raise ValueError('invalid_artifact_reference')
    directory = document_directory(storage, storage_key) / run_id
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise FileNotFoundError('artifact_not_found')
    return path
