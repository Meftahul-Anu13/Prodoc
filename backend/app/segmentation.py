"""Split long documents into invoice segments before extraction.

Large PDFs (up to the 1500-page source cap) cannot go to a model in one
request. Pages without financial content (cover pages, terms, blank scans)
are skipped and listed. The remaining pages are grouped into invoices by
cheap, explainable signals: a new invoice number, "Page 1 of N", or a gap of
skipped pages. Continuation pages ("Page 2 of 3", "continued") stay with the
current invoice. Oversized segments are cut at page boundaries and flagged so
a reviewer can check invoices spanning the cut. Small documents keep the
original single-request behavior.
"""
import os
import re

from .guardrails import pages_of

_INVOICE_NUMBER = re.compile(r"\b(?:invoice|inv|bill)\s*(?:no\.?|number|num\.?|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/.]{2,})", re.IGNORECASE)
_PAGE_OF = re.compile(r"\bpage\s+(\d+)\s*(?:of|/)\s*(\d+)\b", re.IGNORECASE)
_CONTINUED = re.compile(r"\b(continued|cont'd|carried forward)\b", re.IGNORECASE)


def _int_env(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def plan(bundle, report):
    """Return {'segments': [{'pages': [...]}, ...], 'skipped_pages': [...], 'mode': ...}."""
    pages = pages_of(bundle)
    budget = _int_env("SEGMENT_MAX_CHARS", 120_000)
    min_pages = _int_env("SEGMENT_MIN_PAGES", 8)
    total_chars = sum(len(b["text"]) for blocks in pages.values() for b in blocks)
    spreadsheet = any(b.get("location", {}).get("sheet") for b in bundle["blocks"])
    relevant = set(report["relevant_pages"]) if report else set(pages)
    skipped = sorted(set(pages) - relevant)
    if spreadsheet or (len(pages) < min_pages and total_chars <= budget):
        # Small documents and spreadsheets: one request, exactly as before.
        return {"mode": "single", "segments": [{"pages": sorted(pages)}], "skipped_pages": []}
    segments, current, current_number, previous = [], [], None, None
    for page in sorted(relevant):
        text = "\n".join(b["text"] for b in pages[page])
        numbers = [m.group(1).upper() for m in _INVOICE_NUMBER.finditer(text)]
        page_of = _PAGE_OF.search(text)
        first_page = page_of and page_of.group(1) == "1"
        # "Page k of N" may be the whole file's pagination, so only an explicit
        # "continued" marker can override a changed invoice number.
        new_number = numbers and numbers[0] != current_number and not _CONTINUED.search(text)
        gap = previous is not None and page != previous + 1
        if current and (new_number or first_page or gap):
            segments.append({"pages": current, "invoice_hint": current_number})
            current = []
        if not current:
            current_number = numbers[0] if numbers else None
        elif numbers and current_number is None:
            current_number = numbers[0]
        current.append(page)
        previous = page
    if current:
        segments.append({"pages": current, "invoice_hint": current_number})
    # Enforce the per-request size budget at page boundaries.
    bounded = []
    for segment in segments:
        parts, part, size = [], [], 0
        for page in segment["pages"]:
            chars = sum(len(b["text"]) for b in pages[page])
            if part and size + chars > budget:
                parts.append(part)
                part, size = [], 0
            part.append(page)
            size += chars
        parts.append(part)
        # Every piece of a cut invoice is flagged: line items may span the cut.
        bounded.extend({**segment, "pages": p, **({"cut_by_size": True} if len(parts) > 1 else {})} for p in parts)
    return {"mode": "segmented", "segments": bounded, "skipped_pages": skipped}


def batches(bundle, plan):
    """Pack whole invoice segments into model requests.

    A segment is one invoice (never split here); a batch is one request.
    Packing many short invoices per request turns 1000 one-page invoices into
    tens of requests instead of 1000, while each request stays within budget.
    """
    if plan["mode"] == "single":
        return [{"segments": [0], "pages": plan["segments"][0]["pages"]}]
    pages = pages_of(bundle)
    budget = _int_env("BATCH_MAX_CHARS", 24_000)
    # Output time grows with the invoice count: 12 invoices took 316 s to extract on the
    # gateway, over its 280 s limit. Six stays well inside it.
    limit = _int_env("BATCH_MAX_INVOICES", 6)
    result, current, size = [], [], 0
    for index, segment in enumerate(plan["segments"]):
        chars = sum(len(b["text"]) for page in segment["pages"] for b in pages[page])
        if current and (size + chars > budget or len(current) >= limit):
            result.append(current)
            current, size = [], 0
        current.append(index)
        size += chars
    if current:
        result.append(current)
    return [{"segments": group, "pages": [p for i in group for p in plan["segments"][i]["pages"]]} for group in result]


def sub_bundle(bundle, pages):
    wanted = set(pages)
    return {**bundle,
            "blocks": [b for b in bundle["blocks"] if (b.get("location", {}).get("page") or 1) in wanted],
            "pages": [p for p in bundle.get("pages", []) if p.get("page") in wanted]}
