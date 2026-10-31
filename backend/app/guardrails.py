"""Pre-LLM guardrails: is this a financial document, and does it try to instruct the model?

Runs locally on the canonical source blocks *before* any model call, so
irrelevant uploads (CVs, novels, manuals) are rejected without cost or
fabricated "invoices", and instruction-like text is surfaced to reviewers.
Detection is deliberately simple and explainable; it complements, never
replaces, the extraction-side controls (untrusted-data prompts, strict schema,
verbatim source grounding, deterministic validation, human review).
"""
import os
import re

POLICY_VERSION = "guardrails-v1"
KEYWORDS = ("invoice", "receipt", "bill to", "billed to", "ship to", "subtotal", "sub-total", "total", "tax", "vat",
            "gst", "amount due", "balance due", "due date", "payment", "unit price", "qty", "quantity", "credit note",
            "purchase order", "ledger", "debit", "credit", "remit", "iban", "account no")
_KEYWORD = re.compile(r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b", re.IGNORECASE)
_MONEY = re.compile(r"(?<![\w.])(?:[$€£₹¥]\s?)?\d{1,3}(?:[,.' ]\d{3})*[.,]\d{2}(?!\d)")
_CURRENCY = re.compile(r"\b(USD|EUR|GBP|INR|CAD|AUD|JPY|CHF|CNY|AED)\b|[$€£₹¥]")
# Instruction-like text aimed at an AI system. Matches are flagged, never obeyed.
_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|system)\s+(instructions?|prompts?|rules?)"
    r"|\b(system|developer)\s+prompt\b"
    r"|\byou\s+are\s+(now\s+)?(an?\s+)?(ai|assistant|language model|chatgpt|gpt|claude)\b"
    r"|\b(set|change|report|return)\s+(the\s+)?(total|amount|subtotal|tax|invoice number)\s+(to|as)\b"
    r"|\b(do\s+not|don't)\s+(flag|report|validate|review)\b"
    r"|\bjailbreak\b|<\s*/?\s*(system|assistant|instructions?)\s*>",
    re.IGNORECASE)


def page_signals(text):
    keywords = {m.group(1).lower() for m in _KEYWORD.finditer(text)}
    return {"keywords": sorted(keywords), "money_values": len(_MONEY.findall(text)),
            "currency_marks": len(_CURRENCY.findall(text))}


def page_relevance(signals):
    """strong: reads like a financial record; weak: some financial content; none: no signal."""
    keywords, money = len(signals["keywords"]), signals["money_values"]
    if keywords >= 2 and money >= 1:
        return "strong"
    if keywords >= 1 or money >= 3:
        return "weak"
    return "none"


def pages_of(bundle):
    pages = {}
    for block in bundle["blocks"]:
        page = block.get("location", {}).get("page") or 1
        pages.setdefault(page, []).append(block)
    return pages


def assess(bundle):
    """Return the guardrail report for a canonical source bundle (no side effects)."""
    pages = []
    injections = []
    for number, blocks in sorted(pages_of(bundle).items()):
        text = "\n".join(block["text"] for block in blocks)
        signals = page_signals(text)
        pages.append({"page": number, "relevance": page_relevance(signals), **signals})
        for block in blocks:
            match = _INJECTION.search(block["text"])
            if match:
                injections.append({"source_id": block["id"], "page": number, "pattern": match.group(0)[:80]})
    levels = [page["relevance"] for page in pages]
    verdict = "financial" if "strong" in levels else "uncertain" if "weak" in levels else "not_financial"
    return {"policy_version": POLICY_VERSION, "verdict": verdict,
            "relevant_pages": [p["page"] for p in pages if p["relevance"] != "none"],
            "irrelevant_pages": [p["page"] for p in pages if p["relevance"] == "none"],
            "pages": pages, "instruction_like_text": injections}


def mode():
    # enforce: reject non-financial uploads before any model call (default)
    # flag: continue but mark for review; off: skip relevance checks.
    value = os.getenv("GUARDRAIL_RELEVANCE", "enforce").strip().lower()
    return value if value in {"enforce", "flag", "off"} else "enforce"
