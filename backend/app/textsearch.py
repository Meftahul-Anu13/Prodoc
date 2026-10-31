"""Typo-tolerant keyword search with match snippets.

SQLite FTS5 with the trigram tokenizer finds candidate documents, including
for misspelled words (shared trigrams). Each candidate is then scored in
Python: a query word is an *exact* match when it (or its normalized ID/amount
form) occurs in the text, otherwise a *fuzzy* match when a document word is
close enough. All query words must match. Other databases fall back to a
substring scan of the same scoring. The index is derived data: it is rebuilt
from documents.search_text and never the source of truth.
"""
import difflib
import re

from sqlalchemy import text

FUZZY_CUTOFF = 0.8
# Typical scan misreads inside identifiers (letters read for digits).
OCR_FOLD = str.maketrans({"O": "0", "I": "1", "L": "1"})
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/.,]*[A-Za-z0-9]")
_WORD = re.compile(r"\w+")
_AMOUNT = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


def forms(token):
    """Alternative spellings that should match: compact IDs, OCR-folded IDs, plain amounts."""
    result = set()
    if _AMOUNT.match(token):
        result.add(token.replace(",", ""))
    compact = re.sub(r"[^A-Za-z0-9]", "", token).upper()
    if len(compact) >= 4 and any(c.isdigit() for c in compact):
        result.update({compact, compact.translate(OCR_FOLD)})
    return {form for form in result if form and form.casefold() != token.casefold()}


def indexed_content(source):
    # Normalized forms are appended (after a separator line) so that
    # "NW 2026 0417", "nw20260417" and "15362.70" match their source spellings.
    extra = set()
    for token in _TOKEN.findall(source):
        extra |= forms(token)
    return source + ("\n␞ " + " ".join(sorted(extra)) if extra else "")


def _sqlite(session):
    return session.get_bind().dialect.name == "sqlite"


def ensure(session):
    if _sqlite(session):
        session.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING "
                             "fts5(document_id UNINDEXED, owner_id UNINDEXED, content, tokenize='trigram')"))


def upsert(session, document):
    if not _sqlite(session):
        return
    ensure(session)
    session.execute(text("DELETE FROM search_fts WHERE document_id = :id"), {"id": document.id})
    if document.search_text:
        session.execute(text("INSERT INTO search_fts(document_id, owner_id, content) VALUES (:id, :owner, :content)"),
                        {"id": document.id, "owner": document.owner_id, "content": indexed_content(document.search_text)})


def remove(session, document_id):
    if _sqlite(session):
        ensure(session)
        session.execute(text("DELETE FROM search_fts WHERE document_id = :id"), {"id": document_id})


def backfill(session):
    """Index documents that have text but no index row (older data, missed hooks)."""
    if not _sqlite(session):
        return 0
    ensure(session)
    rows = session.execute(text("SELECT id, owner_id, search_text FROM documents WHERE search_text IS NOT NULL "
                                "AND search_text != '' AND id NOT IN (SELECT document_id FROM search_fts)")).all()
    for identifier, owner, source in rows:
        session.execute(text("INSERT INTO search_fts(document_id, owner_id, content) VALUES (:id, :owner, :content)"),
                        {"id": identifier, "owner": owner, "content": indexed_content(source)})
    return len(rows)


def query_words(query):
    # Words under 3 characters ("of", "to") carry little signal and cannot use trigrams.
    return [word for word in dict.fromkeys(_TOKEN.findall(query) or _WORD.findall(query)) if len(word) >= 3]


def _trigrams(word):
    lowered = word.casefold()
    return {lowered[i:i + 3] for i in range(len(lowered) - 2)}


def _candidates(session, words, owner_id, admin, limit=300):
    owner = "" if admin else " AND owner_id = :owner"
    params = {"owner": owner_id, "limit": limit}
    if _sqlite(session):
        grams = set()
        for word in words:
            grams |= _trigrams(word)
            for form in forms(word):
                grams |= _trigrams(form)
        expression = " OR ".join('"' + gram.replace('"', '""') + '"' for gram in sorted(grams))
        params["match"] = expression
        return session.execute(text("SELECT document_id, content FROM search_fts WHERE search_fts MATCH :match"
                                    + owner + " ORDER BY bm25(search_fts) LIMIT :limit"), params).all()
    # Non-SQLite fallback: scan the owner's documents (bounded).
    return session.execute(text("SELECT id, search_text FROM documents WHERE search_text IS NOT NULL"
                                + owner + " LIMIT :limit"), params).all()


def _score_word(word, lowered_content, vocabulary):
    """Return (kind, score, surface) for one query word, or None if it does not match."""
    if word.casefold() in lowered_content:
        return "exact", 1.0, word
    for form in forms(word) | forms(word.upper()):
        if form.casefold() in lowered_content:
            return "exact", 1.0, form
    close = difflib.get_close_matches(word.casefold(), vocabulary, n=1, cutoff=FUZZY_CUTOFF)
    if close:
        ratio = difflib.SequenceMatcher(None, word.casefold(), close[0]).ratio()
        return "fuzzy", 0.9 * ratio, close[0]
    return None


def readable(source):
    """Parser Markdown (bold markers, table pipes/rules, headings) is noise in a snippet."""
    cleaned = re.sub(r"\*\*|__", "", source)
    cleaned = re.sub(r"(?m)^[ \t]*\|?[ \t]*:?-{3,}.*$", "", cleaned)
    cleaned = re.sub(r"(?m)^#+[ \t]*", "", cleaned)
    # Cell separators become spacing; row breaks stay so rows read as separate lines.
    cleaned = re.sub(r"(?m)^[ \t]*\|[ \t]*|[ \t]*\|[ \t]*$", "", cleaned)
    cleaned = re.sub(r"[ \t]*\|[ \t]*", "  ", cleaned)
    return re.sub(r"[ \t]{3,}", "  ", cleaned)


def clean_line(chunk):
    """Readable one-line text for a semantic match (no Markdown, single spaces).

    Chunks join several lines, so table rules are removed inline rather than
    as whole lines (which would drop the rows sharing that joined line).
    """
    cleaned = re.sub(r"\*\*|__", "", chunk)
    cleaned = re.sub(r"(?:\|?\s*:?-{3,}:?\s*)+\|", " ", cleaned)
    cleaned = re.sub(r"(?:^|\s)#+\s", " ", cleaned)
    cleaned = re.sub(r"\s*\|\s*", "  ", cleaned)
    return re.sub(r"\s{3,}", "  ", cleaned).strip()


def source_spellings(source, surfaces):
    """Map normalized matches (NW20260417, 15362.70) back to how the document wrote them."""
    wanted = {s.casefold() for s in surfaces if s}
    found = []
    for token in _TOKEN.findall(source):
        if {f.casefold() for f in forms(token)} & wanted:
            found.append(token)
    return found


def snippet(source, surfaces, width=180):
    """A short window around the first match with highlight ranges, newlines shown as ' · '."""
    source = readable(source)
    surfaces = [s for s in surfaces if s] + source_spellings(source, surfaces)
    lowered = source.casefold()
    positions = [lowered.find(s.casefold()) for s in surfaces if lowered.find(s.casefold()) >= 0]
    position = min(positions) if positions else 0
    start = max(0, position - 60)
    if start:
        boundary = source.find(" ", start, position)
        start = boundary + 1 if boundary >= 0 else start
    end = min(len(source), start + width)
    window = re.sub(r"\s*\n\s*", " · ", source[start:end]).strip(" ·")
    window = re.sub(r"(?: · ){2,}", " · ", window)
    prefix, suffix = ("… " if start else ""), (" …" if end < len(source) else "")
    window = prefix + window + suffix
    highlights, lowered_window = [], window.casefold()
    for surface in sorted({s.casefold() for s in surfaces}, key=len, reverse=True):
        index = lowered_window.find(surface)
        while index >= 0:
            if not any(a < index + len(surface) and index < b for a, b in highlights):
                highlights.append([index, index + len(surface)])
            index = lowered_window.find(surface, index + len(surface))
    return window, sorted(highlights)


def keyword_hits(session, query, owner_id, admin=False, limit=50):
    """Documents matching every query word, exact matches ranked above fuzzy ones."""
    words = query_words(query)
    if not words:
        return []
    backfill(session)
    hits = []
    for document_id, content in _candidates(session, words, owner_id, admin):
        source = content.split("\n␞ ", 1)[0]
        lowered = content.casefold()
        vocabulary = {w for w in _WORD.findall(lowered) if abs(len(w) - max(len(x) for x in words)) <= 6}
        scored = [_score_word(word, lowered, vocabulary) for word in words]
        if any(item is None for item in scored):
            continue
        kind = "exact" if all(item[0] == "exact" for item in scored) else "fuzzy"
        score = sum(item[1] for item in scored) / len(scored)
        if query.casefold() in source.casefold():
            score += 0.5  # whole phrase present
        window, highlights = snippet(source, [query] + [item[2] for item in scored])
        hits.append({"document_id": document_id, "type": kind, "score": round(score, 4),
                     "snippet": window, "highlights": highlights})
    hits.sort(key=lambda hit: (hit["type"] != "exact", -hit["score"]))
    return hits[:limit]
