"""Local ONNX embeddings and rebuildable Qdrant index; no text in payloads."""
import os
from functools import lru_cache
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models

COLLECTION = 'document_chunks_v2'
# Bump when chunking/embedding changes; documents indexed under another version are re-indexed.
INDEX_VERSION = 2


@lru_cache(maxsize=1)
def embedding_model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name='BAAI/bge-small-en-v1.5', threads=2)


def client():
    # QDRANT_PATH selects Qdrant's embedded local mode (a folder, no server).
    # Only one process may open it, so the API owns indexing in that mode.
    path = os.getenv('QDRANT_PATH')
    if path:
        return _local_client(path)
    return QdrantClient(url=os.getenv('QDRANT_URL', 'http://localhost:6333'), timeout=5)


@lru_cache(maxsize=1)
def _local_client(path):
    return QdrantClient(path=path)


def collection_ready():
    try:
        return client().collection_exists(COLLECTION)
    except Exception:
        return False


def indexer():
    # 'api' when the index is embedded: the extraction worker must not open it.
    return 'api' if os.getenv('QDRANT_PATH') else os.getenv('SEMANTIC_INDEXER', 'worker')


def enabled():
    return os.getenv('SEMANTIC_ENABLED', 'false').lower() == 'true'


def chunks(text, size=150):
    # Line-level chunks: an invoice line ("Shipping service 25.00") keeps its own
    # meaning instead of being averaged into a whole page of generic invoice text.
    offset, start, buffer = 0, 0, ''
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            if buffer and len(buffer) + len(stripped) > size:
                yield start, buffer
                buffer = ''
            if not buffer:
                start = offset
            # Overlong lines are split rather than embedded past the model's useful length.
            while len(stripped) > size * 4:
                yield offset, stripped[:size * 4]
                stripped = stripped[size * 4:]
            buffer = (buffer + ' ' + stripped).strip()
        offset += len(line)
    if buffer:
        yield start, buffer


def index_document(document_id, owner_id, text):
    if not enabled():
        return False
    q = client()
    if not q.collection_exists(COLLECTION):
        q.create_collection(COLLECTION, vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE))
    delete_document(document_id)
    batch = []
    for offset, chunk in chunks(text):
        vector = next(embedding_model().passage_embed([chunk])).tolist()
        batch.append(models.PointStruct(id=str(uuid5(NAMESPACE_URL, f'{document_id}:{offset}')), vector=vector,
                                      payload={'document_id': document_id, 'owner_id': owner_id, 'offset': offset}))
        if len(batch) == 32:
            q.upsert(COLLECTION, batch, wait=True)
            batch = []
    if batch:
        q.upsert(COLLECTION, batch, wait=True)
    return True


def _threshold(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def semantic_hits(query, owner_id, admin=False, limit=50):
    """Documents semantically close to the query, best first, with the best chunk offset.

    Nearest-neighbour search always returns *something*, so results are cut
    by an absolute similarity floor and by distance from the best match.
    bge-small cosine scores are compressed (~0.4-0.85); the defaults were
    calibrated on a small local sample and are configurable.
    """
    if not enabled():
        raise RuntimeError('semantic_disabled')
    q = client()
    # BGE expects queries with its retrieval instruction (query_embed adds it).
    vector = next(embedding_model().query_embed([query])).tolist()
    filters = None if admin else models.Filter(must=[models.FieldCondition(key='owner_id', match=models.MatchValue(value=owner_id))])
    results = q.query_points(COLLECTION, query=vector, query_filter=filters, limit=limit, with_payload=True).points
    best = {}
    for point in results:
        document = point.payload['document_id']
        if point.score > best.get(document, (-1.0, 0))[0]:
            best[document] = (point.score, point.payload.get('offset', 0))
    if not best:
        return []
    floor = _threshold('SEMANTIC_MIN_SCORE', '0.60')
    cutoff = max(score for score, _ in best.values()) - _threshold('SEMANTIC_MAX_GAP', '0.04')
    ranked = sorted(best.items(), key=lambda item: -item[1][0])
    return [{'document_id': document, 'score': round(score, 4), 'offset': offset}
            for document, (score, offset) in ranked if score >= floor and score >= cutoff]


def semantic_ids(query, owner_id, admin=False, limit=50):
    return [hit['document_id'] for hit in semantic_hits(query, owner_id, admin, limit)]


def chunk_at(text, offset):
    """The indexed chunk text that starts at offset (for showing why a document matched)."""
    for start, chunk in chunks(text or ''):
        if start == offset:
            return chunk
    return ''


def delete_document(document_id):
    if enabled():
        q = client()
        if q.collection_exists(COLLECTION):
            q.delete(COLLECTION, points_selector=models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(key='document_id', match=models.MatchValue(value=document_id))])), wait=True)


def rrf(*rankings):
    scores = {}
    for ranking in rankings:
        for rank, identifier in enumerate(ranking, 1):
            scores[identifier] = scores.get(identifier, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))
