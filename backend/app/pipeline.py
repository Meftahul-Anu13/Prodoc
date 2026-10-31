"""Bounded document ingestion and a deterministic, offline invoice baseline.

Document text is untrusted data, never instructions. No network calls are made.
Missing fields stay None; extraction evidence preserves exact source fragments.
Dependencies are imported by adapter so unavailable OCR/XLS support fails explicitly.
"""
from __future__ import annotations

import bisect
import copy
import csv
import io
import math
import os
import re
import unicodedata
import warnings
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_PAGES = 1500
MAX_IMAGE_PIXELS = 40_000_000
MAX_TOTAL_OCR_PIXELS = 500_000_000
MAX_ROWS = 100_000
MAX_CELLS = 1_000_000
MAX_TEXT_CHARS = 20_000_000
MAX_ZIP_BYTES = 128 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000
OCR_TIMEOUT_SECONDS = 45
SCHEMA_VERSION = "1.0"
RULE_VERSION = "decimal-v1"
FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name", "currency", "subtotal", "tax", "total")
MONEY_FIELDS = ("subtotal", "tax", "total")
SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".xlsx", ".xls", ".csv"}


class PipelineError(ValueError):
    """A controlled ingestion failure safe for the job runner to record."""


def _finding(rule_id: str, message: str, severity: str = "warning", **extra: Any) -> dict:
    return {"rule_id": rule_id, "severity": severity, "message": message, "rule_version": RULE_VERSION, **extra}


def _decimal(value: Any) -> Decimal:
    """Parse ordinary US/EU money without binary arithmetic or silent coercion."""
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing or invalid numeric value")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        number = Decimal(str(value))
    else:
        text = str(value).strip().replace(" ", " ").replace(" ", " ")
        text = re.sub(r"(?i)\b(?:USD|EUR|GBP|INR|CAD|AUD|JPY|CHF|CNY|AED)\b", "", text)
        text = re.sub(r"[$€£₹¥]", "", text).strip()
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1].strip()
        if not re.fullmatch(r"[+-]?\d[\d ,.']*", text) or len(text) > 50:
            raise ValueError("Invalid numeric value")
        # Spaces and apostrophes are accepted only as grouped thousands.
        if " " in text or "'" in text:
            if not re.fullmatch(r"[+-]?\d{1,3}(?:[ ' ]\d{3})+(?:[.,]\d+)?", text):
                raise ValueError("Invalid digit grouping")
            text = text.replace(" ", "").replace("'", "")
        if "," in text and "." in text:
            decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
            thousands_sep = "." if decimal_sep == "," else ","
            whole, fraction = text.rsplit(decimal_sep, 1)
            if not re.fullmatch(r"[+-]?\d{1,3}(?:" + re.escape(thousands_sep) + r"\d{3})+", whole) or not fraction.isdigit():
                raise ValueError("Invalid digit grouping")
            text = whole.replace(thousands_sep, "") + "." + fraction
        elif "," in text:
            if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+", text):
                text = text.replace(",", "")
            elif text.count(",") == 1:
                text = text.replace(",", ".")
            else:
                raise ValueError("Invalid decimal separators")
        elif text.count(".") > 1:
            if re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3})+", text):
                text = text.replace(".", "")
            else:
                raise ValueError("Invalid decimal separators")
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("Invalid numeric value") from exc
        if negative:
            number = -number
    if not number.is_finite() or abs(number) > Decimal("1000000000000000000") or number.as_tuple().exponent < -6:
        raise ValueError("Numeric value outside supported bounds")
    return number


def _money(value: Any) -> str:
    number = _decimal(value)
    text = format(number, "f")
    if "." not in text:
        return text + ".00"
    return text + "0" * max(0, 2 - len(text.split(".")[1]))


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _base_invoice() -> dict:
    return {**dict.fromkeys(FIELDS), "line_items": [], "evidence": {}, "findings": [], "needs_review": False}


def _location(source: dict, source_text: str, **extra: Any) -> dict:
    return {key: value for key, value in {"page": source.get("page"), "sheet": source.get("sheet"), "source_text": source_text, **extra}.items() if value is not None}


_RAPID_ENGINE = None
MAX_OCR_SIDE = 2048


def _ocr(image: Any) -> str | dict:
    """Explicit local engine selection; no automatic fallback or network calls.

    RapidOCR models must be provisioned during setup, never during processing.
    Downsampling and ONNX thread limits bound per-page CPU/memory; the worker
    additionally owns its job timeout. English PP-OCRv4 mobile models are used.
    Tesseract is optional and has a per-call timeout.
    """
    engine_name = os.getenv("OCR_ENGINE", "rapidocr").strip().lower()
    if engine_name == "tesseract":
        try:
            import pytesseract
        except ImportError as exc:
            raise PipelineError("OCR_UNAVAILABLE: install pytesseract and the Tesseract system executable") from exc
        try:
            return {"text": pytesseract.image_to_string(image, config="--psm 6", timeout=OCR_TIMEOUT_SECONDS), "engine": "tesseract", "boxes": [], "confidence": None}
        except Exception as exc:
            raise PipelineError("OCR_FAILED: Tesseract unavailable, timed out, or could not read this page") from exc
    if engine_name != "rapidocr":
        raise PipelineError("OCR_ENGINE: select rapidocr or tesseract explicitly")
    try:
        import numpy as np
        import rapidocr
        from rapidocr import RapidOCR, LangRec, ModelType, OCRVersion
    except ImportError as exc:
        raise PipelineError("OCR_UNAVAILABLE: install rapidocr and opencv-python-headless") from exc
    global _RAPID_ENGINE
    try:
        if _RAPID_ENGINE is None:
            model_dir = Path(os.getenv("OCR_MODEL_DIR", str(Path(rapidocr.__file__).parent / "models")))
            detector = model_dir / "ch_PP-OCRv4_det_mobile.onnx"
            recognizer = model_dir / "en_PP-OCRv4_rec_mobile.onnx"
            if not detector.is_file() or not recognizer.is_file():
                raise PipelineError("OCR_MODELS_MISSING: provision PP-OCRv4 mobile detector and English recognizer during setup")
            _RAPID_ENGINE = RapidOCR(params={
                "Global.use_cls": False, "Global.log_level": "warning",
                "Det.ocr_version": OCRVersion.PPOCRV4, "Det.model_type": ModelType.MOBILE,
                "Det.model_path": str(detector), "Det.limit_type": "max", "Det.limit_side_len": MAX_OCR_SIDE,
                "Rec.ocr_version": OCRVersion.PPOCRV4, "Rec.model_type": ModelType.MOBILE, "Rec.lang_type": LangRec.EN,
                "Rec.model_path": str(recognizer),
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "EngineConfig.onnxruntime.use_cuda": False,
            })
        with image.convert("RGB") as frame:
            original_width, original_height = frame.size
            frame.thumbnail((MAX_OCR_SIDE, MAX_OCR_SIDE))
            scale_x, scale_y = original_width / frame.width, original_height / frame.height
            # RapidOCR consumes OpenCV's BGR channel order.
            result = _RAPID_ENGINE(np.asarray(frame)[:, :, ::-1].copy())
        boxes = []
        rows = zip(result.boxes, result.txts, result.scores) if result.boxes is not None and result.txts is not None and result.scores is not None else []
        for box, text, score in rows:
            boxes.append({"bbox": [[float(x) * scale_x, float(y) * scale_y] for x, y in box], "text": str(text), "confidence": float(score)})
        # Preserve horizontal table boundaries instead of flattening every box
        # onto its own line. Boxes are grouped geometrically, then left-to-right.
        lines: list[list[dict]] = []
        for entry in sorted(boxes, key=lambda item: min(point[1] for point in item["bbox"])):
            top, bottom = min(p[1] for p in entry["bbox"]), max(p[1] for p in entry["bbox"])
            center, height = (top + bottom) / 2, max(bottom - top, 1)
            if lines:
                previous = lines[-1][0]["bbox"]
                previous_center = sum(p[1] for p in previous) / len(previous)
                same_line = abs(center - previous_center) < height * 0.6
            else:
                same_line = False
            if not same_line:
                lines.append([])
            lines[-1].append(entry)
        text = "\n".join("  ".join(entry["text"] for entry in sorted(line, key=lambda entry: min(p[0] for p in entry["bbox"]))) for line in lines)
        return {"text": text, "engine": "rapidocr", "boxes": boxes, "confidence": min((entry["confidence"] for entry in boxes), default=None), "confidence_calibrated": False}
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("OCR_FAILED: local RapidOCR could not initialize or read this page") from exc


def _attach_ocr(source: dict, image: Any) -> None:
    output = _ocr(image)
    if isinstance(output, dict):
        source["text"] = output["text"]
        source["ocr"] = {key: value for key, value in output.items() if key != "text"}
    else:
        source["text"] = output  # Also supports lightweight adapter test doubles.


def _check_pixels(width: int, height: int) -> int:
    pixels = width * height
    if pixels <= 0 or pixels > MAX_IMAGE_PIXELS:
        raise PipelineError("IMAGE_LIMIT: page exceeds the allowed pixel count")
    return pixels


def _pdf_sources(path: Path) -> list[dict]:
    import fitz
    from PIL import Image

    sources = []
    ocr_pixels = 0
    text_chars = 0
    try:
        with fitz.open(path) as document:
            if document.needs_pass:
                raise PipelineError("ENCRYPTED_PDF: password protected documents are unsupported")
            if not 0 < len(document) <= MAX_PAGES:
                raise PipelineError("PAGE_LIMIT: PDF is empty or exceeds the page limit")
            for index in range(len(document)):
                source = {"page": index + 1, "text": "", "method": "digital"}
                try:
                    page = document[index]
                    # PyMuPDF's sorted text mode collapses explicit column spaces
                    # on some versions. Preserve native text for grounded tables.
                    text = page.get_text("text", sort=False)
                    if len(re.sub(r"\W", "", text)) >= 15:
                        source["text"] = text
                    else:
                        source["method"] = "ocr"
                        pixels = _check_pixels(math.ceil(page.rect.width * 2), math.ceil(page.rect.height * 2))
                        ocr_pixels += pixels
                        if ocr_pixels > MAX_TOTAL_OCR_PIXELS:
                            raise PipelineError("OCR_LIMIT: document exceeds the OCR pixel budget")
                        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, colorspace=fitz.csRGB)
                        with Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples) as image:
                            _attach_ocr(source, image)
                        if not source["text"].strip():
                            source["error"] = "EMPTY_OCR: no readable text recovered"
                except Exception as exc:
                    source["error"] = str(exc) if isinstance(exc, PipelineError) else "PAGE_FAILED: unable to read PDF page"
                text_chars += len(source["text"])
                if text_chars > MAX_TEXT_CHARS:
                    raise PipelineError("TEXT_LIMIT: document text exceeds the limit")
                sources.append(source)
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("CORRUPT_PDF: unable to open PDF") from exc
    return sources


def _image_sources(path: Path) -> list[dict]:
    from PIL import Image, ImageOps

    sources = []
    total_pixels = 0
    text_chars = 0
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                count = getattr(image, "n_frames", 1)
                if not 0 < count <= MAX_PAGES:
                    raise PipelineError("PAGE_LIMIT: image frame count exceeds the limit")
                for index in range(count):
                    image.seek(index)
                    total_pixels += _check_pixels(*image.size)
                    if total_pixels > MAX_TOTAL_OCR_PIXELS:
                        raise PipelineError("OCR_LIMIT: document exceeds the OCR pixel budget")
                    source = {"page": index + 1, "text": "", "method": "ocr"}
                    try:
                        with ImageOps.exif_transpose(image).convert("RGB") as frame:
                            _attach_ocr(source, frame)
                        if not source["text"].strip():
                            source["error"] = "EMPTY_OCR: no readable text recovered"
                    except PipelineError as exc:
                        source["error"] = str(exc)
                    text_chars += len(source["text"])
                    if text_chars > MAX_TEXT_CHARS:
                        raise PipelineError("TEXT_LIMIT: document text exceeds the limit")
                    sources.append(source)
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("CORRUPT_IMAGE: invalid, oversized, or unreadable image") from exc
    return sources


def _sheet_source(name: str, rows: Any, page: int, counters: list[int], hidden: bool = False) -> dict:
    source = {"page": page, "sheet": name, "text": "", "method": "spreadsheet", "rows": [], "hidden": hidden}
    lines = []
    for row_number, raw_row in enumerate(rows, 1):
        counters[0] += 1
        counters[1] += len(raw_row)
        if counters[0] > MAX_ROWS or counters[1] > MAX_CELLS:
            raise PipelineError("SHEET_LIMIT: workbook exceeds row or cell limits")
        values = [_text(value) for value in raw_row]
        if not any(values):
            continue
        text = "\t".join(values)
        counters[2] += len(text)
        if counters[2] > MAX_TEXT_CHARS:
            raise PipelineError("TEXT_LIMIT: spreadsheet text exceeds the limit")
        source["rows"].append({"row": row_number, "values": values})
        lines.append(text)
    source["text"] = "\n".join(lines)
    return source


def _xlsx_sources(path: Path) -> list[dict]:
    from openpyxl import load_workbook

    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES or sum(entry.file_size for entry in entries) > MAX_ZIP_BYTES:
                raise PipelineError("ZIP_LIMIT: XLSX expanded size or member count exceeds the limit")
            if any(entry.file_size > 1024 * 1024 and entry.file_size / max(entry.compress_size, 1) > 250 for entry in entries):
                raise PipelineError("ZIP_LIMIT: suspicious XLSX compression ratio")
            if "xl/workbook.xml" not in archive.namelist():
                raise PipelineError("CORRUPT_XLSX: workbook manifest is missing")
        # data_only prevents executing formulas; cached results are used if present.
        workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            if len(workbook.worksheets) > MAX_PAGES:
                raise PipelineError("PAGE_LIMIT: too many workbook sheets")
            sources, counters = [], [0, 0, 0]
            for index, sheet in enumerate(workbook.worksheets, 1):
                if (sheet.max_row or 0) > MAX_ROWS or (sheet.max_row or 0) * (sheet.max_column or 0) > MAX_CELLS:
                    raise PipelineError("SHEET_LIMIT: worksheet dimensions exceed limits")
                sources.append(_sheet_source(sheet.title, sheet.iter_rows(values_only=True), index, counters, sheet.sheet_state != "visible"))
            return sources
        finally:
            workbook.close()
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("CORRUPT_XLSX: unable to read workbook") from exc


def _xls_sources(path: Path) -> list[dict]:
    try:
        import xlrd
    except ImportError as exc:
        raise PipelineError("XLS_UNAVAILABLE: install xlrd to read legacy workbooks") from exc
    try:
        workbook = xlrd.open_workbook(str(path), on_demand=True)
        try:
            if workbook.nsheets > MAX_PAGES:
                raise PipelineError("PAGE_LIMIT: too many workbook sheets")
            sources, counters = [], [0, 0, 0]
            for index in range(workbook.nsheets):
                sheet = workbook.sheet_by_index(index)
                if sheet.nrows > MAX_ROWS or sheet.nrows * sheet.ncols > MAX_CELLS:
                    raise PipelineError("SHEET_LIMIT: worksheet dimensions exceed limits")
                def rows():
                    for row in range(sheet.nrows):
                        yield [xlrd.xldate.xldate_as_datetime(cell.value, workbook.datemode).date() if cell.ctype == xlrd.XL_CELL_DATE else cell.value for cell in sheet.row(row)]
                sources.append(_sheet_source(sheet.name, rows(), index + 1, counters, bool(sheet.visibility)))
            return sources
        finally:
            workbook.release_resources()
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("CORRUPT_XLS: unable to read legacy workbook") from exc


def _csv_sources(path: Path) -> list[dict]:
    from charset_normalizer import from_bytes

    data = path.read_bytes()
    if b"\x00" in data and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise PipelineError("CSV_ENCODING: input appears to be binary rather than CSV")
    try:
        # Prefer UTF-8; detection is an explicit fallback for legacy exports.
        try:
            text, encoding = data.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            match = from_bytes(data).best()
            if match is None:
                raise PipelineError("CSV_ENCODING: unable to determine CSV encoding")
            text, encoding = str(match), match.encoding
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        source = _sheet_source("CSV", csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True), 1, [0, 0, 0])
        source.update(method="csv", encoding=encoding)
        return [source]
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("CORRUPT_CSV: unable to decode or parse CSV") from exc


def extract_sources(path: Path, filename: str) -> list[dict]:
    """Return page/sheet text with location and explicit per-page OCR errors.

    Fatal format/resource failures raise PipelineError. OCR failure is retained on
    its page so a document can preserve other, successfully extracted pages.
    """
    path = Path(path)
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise PipelineError("UNSUPPORTED_FORMAT: supported formats are PDF, JPG, PNG, TIFF, XLSX, XLS, CSV")
    if not path.is_file():
        raise PipelineError("FILE_MISSING: source file does not exist")
    if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
        raise PipelineError("FILE_LIMIT: file is empty or exceeds 100 MB")
    if suffix == ".pdf":
        return _pdf_sources(path)
    if suffix in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
        return _image_sources(path)
    if suffix == ".xlsx":
        return _xlsx_sources(path)
    if suffix == ".xls":
        return _xls_sources(path)
    return _csv_sources(path)


# Header aliases are intentionally conservative: a bare 'name' or 'amount' is
# ambiguous and must not become a guessed supplier or invoice total.
ALIASES = {
    "invoice_number": {"invoice number", "invoice no", "invoice #", "invoice id", "inv no", "inv number", "invoice"},
    "invoice_date": {"invoice date", "date", "issue date", "issued on"},
    "supplier_name": {"supplier", "supplier name", "vendor", "vendor name", "seller", "from"},
    "customer_name": {"customer", "customer name", "bill to", "billed to", "buyer", "buyer name"},
    "currency": {"currency", "currency code"},
    "subtotal": {"subtotal", "sub total", "net amount", "net total"},
    "tax": {"tax", "tax amount", "vat", "vat amount", "gst", "gst amount", "sales tax"},
    "total": {"total", "grand total", "invoice total", "total amount", "amount due", "balance due"},
    "description": {"description", "item", "item description", "product", "service"},
    "quantity": {"quantity", "qty"},
    "unit_price": {"unit price", "unit cost", "price", "rate"},
    "line_total": {"line total", "line amount", "item total", "extended amount"},
}


def _normalize(value: Any) -> str:
    value = unicodedata.normalize("NFKC", _text(value)).casefold().strip()
    return re.sub(r"\s+", " ", re.sub(r"[_\-.:]+", " ", value)).strip()


def _header(value: Any) -> str | None:
    normalized = _normalize(value)
    return next((field for field, aliases in ALIASES.items() if normalized in aliases), None)


def _set_field(invoice: dict, field: str, raw: str, location: dict) -> None:
    raw = raw.strip()
    if not raw:
        return
    value: Any = raw
    if field in MONEY_FIELDS:
        try:
            value = _money(raw)
        except ValueError:
            invoice["findings"].append(_finding("invalid_numeric", f"{field} is not a valid monetary value", "error", field=field, evidence=location))
            return
    elif field == "currency":
        value = raw.upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            invoice["findings"].append(_finding("ambiguous_currency", "Currency must be an explicit three-letter code", field=field))
            return
    if invoice.get(field) is not None and invoice[field] != value:
        invoice["findings"].append(_finding("conflicting_values", f"Conflicting values for {field}; manual review required", "error", field=field, evidence=location))
        # Preserve alternatives instead of retaining a potentially incorrect first value.
        invoice.setdefault("conflicts", {}).setdefault(field, []).append({"value": value, "evidence": location})
    else:
        invoice[field] = value
        invoice["evidence"][field] = location


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _tabular_invoices(source: dict) -> list[dict]:
    invoices: list[dict] = []
    mapping: dict[int, str] = {}
    for row in source["rows"]:
        values = row["values"]
        candidate = {index: field for index, value in enumerate(values) if (field := _header(value))}
        if (("invoice_number" in candidate.values() and len(candidate) >= 2)
                or (len(candidate) >= 3 and any(field in candidate.values() for field in MONEY_FIELDS))):
            mapping = candidate
            continue
        if not mapping:
            continue
        # A row is an invoice only when it has identifying or invoice monetary data.
        if not any(index < len(values) and values[index].strip() for index, field in mapping.items() if field in ("invoice_number", "total", "subtotal")):
            continue
        invoice = _base_invoice()
        item, item_evidence = {}, {}
        for index, field in mapping.items():
            if index >= len(values) or not values[index].strip():
                continue
            raw = values[index]
            location = _location(source, raw, row=row["row"], cell=f"{_column_name(index + 1)}{row['row']}")
            if field in FIELDS:
                _set_field(invoice, field, raw, location)
            else:
                try:
                    item[field] = raw if field == "description" else _money(raw)
                    item_evidence[field] = location
                except ValueError:
                    invoice["findings"].append(_finding("invalid_numeric", f"Invalid line item {field}", "error", evidence=location))
        if item:
            item["evidence"] = item_evidence
            invoice["line_items"].append(item)
            invoice["evidence"]["line_items"] = _location(source, "\t".join(values), row=row["row"])
        # Deliberately one record per row; no speculative grouping or total summing.
        invoice["extraction_method"] = "deterministic_header_mapping"
        invoices.append(invoice)
    if not invoices and source["text"].strip():
        invoices = _text_invoices(source)
    return invoices


LABEL_PATTERNS = {
    "invoice_number": r"(?:invoice\s*(?:number|no\.?|#|id)|inv\.?\s*(?:number|no\.?|#)|invoice(?=\s*[:=]))",
    "invoice_date": r"(?:invoice\s+date|issue\s+date|issued\s+on|date)",
    "supplier_name": r"(?:supplier(?:\s+name)?|vendor(?:\s+name)?|seller|from)",
    "customer_name": r"(?:customer(?:\s+name)?|bill(?:ed)?\s+to|buyer(?:\s+name)?)",
    "currency": r"currency(?:\s+code)?",
    "subtotal": r"(?:sub\s*total|net\s+(?:amount|total))",
    "tax": r"(?:tax(?:\s+amount)?|sales\s+tax|vat(?:\s+amount)?|gst(?:\s+amount)?)(?:\s*\(?\d+(?:\.\d+)?%\)?)?",
    "total": r"(?:grand\s+total|invoice\s+total|total\s+amount|amount\s+due|balance\s+due|total)",
}
LABELS = {field: re.compile(r"^\s*" + pattern + r"\s*(?::|=|\t|\s)\s*(.*?)\s*$", re.I) for field, pattern in LABEL_PATTERNS.items()}
INVOICE_START = re.compile(r"^\s*(?:invoice\s*(?:number|no\.?|#|id)|inv\.?\s*(?:no\.?|#)|invoice(?=\s*[:=]))\s*[:=]?\s*(\S.*?)\s*$", re.I)


def _parse_text_segment(source: dict, lines: list[str]) -> dict:
    invoice = _base_invoice()
    invoice["extraction_method"] = "deterministic_label_parser"
    for index, line in enumerate(lines):
        for field, pattern in LABELS.items():
            match = pattern.match(line)
            if not match:
                continue
            raw = match.group(1).strip()
            evidence_text = line.strip()
            if not raw and index + 1 < len(lines):
                following = lines[index + 1].strip()
                if following and not any(pattern.match(following) for pattern in LABELS.values()):
                    raw = following
                    evidence_text += "\n" + following
            if raw:
                _set_field(invoice, field, raw, _location(source, evidence_text))
                if field in MONEY_FIELDS:
                    codes = set(re.findall(r"\b(?:USD|EUR|GBP|INR|CAD|AUD|JPY|CHF|CNY|AED)\b", raw.upper()))
                    if len(codes) == 1:
                        _set_field(invoice, "currency", codes.pop(), _location(source, evidence_text))
            break
    # Text-table line items are parsed only with explicit column boundaries.
    header = None
    item_locations = []
    for line in lines:
        cells = [cell.strip() for cell in re.split(r"\t+|\s*\|\s*| {2,}", line.strip())]
        mapped = {i: _header(cell) for i, cell in enumerate(cells) if _header(cell)}
        if "description" in mapped.values() and "line_total" in mapped.values():
            header = mapped
            continue
        if header and len(cells) > max(header) and not any(pattern.match(line) for pattern in LABELS.values()):
            item = {}
            try:
                for i, field in header.items():
                    if field in {"description", "quantity", "unit_price", "line_total"}:
                        item[field] = cells[i] if field == "description" else _money(cells[i])
                item["evidence"] = _location(source, line.strip())
                invoice["line_items"].append(item)
                item_locations.append(line.strip())
            except ValueError:
                invoice["findings"].append(_finding("invalid_line_item", "A table row contains invalid numeric values", "error", evidence=_location(source, line.strip())))
    if item_locations:
        invoice["evidence"]["line_items"] = _location(source, "\n".join(item_locations))
    return invoice


def _text_invoices(source: dict) -> list[dict]:
    segments: list[list[str]] = []
    current: list[str] = []
    identity = None
    for line in source["text"].splitlines():
        match = INVOICE_START.match(line)
        if match and identity is not None and _normalize(match.group(1)) != identity:
            segments.append(current)
            current = []
        if match:
            identity = _normalize(match.group(1))
        current.append(line)
    if current:
        segments.append(current)
    invoices = [_parse_text_segment(source, lines) for lines in segments]
    return [invoice for invoice in invoices if any(invoice[field] is not None for field in FIELDS)]


def validate_invoice(invoice: dict) -> list[dict]:
    """Validate without mutation; preserve the legacy benchmark as an explicit policy."""
    if invoice.get('validation_policy') == 'source_backed_v2':
        from .financial import validate_source_invoice
        return validate_source_invoice(invoice)
    findings = []
    for field in FIELDS:
        if invoice.get(field) is None or invoice.get(field) == "":
            findings.append(_finding("missing_field", f"Missing {field}", field=field))
    parsed = {}
    for field in MONEY_FIELDS:
        if invoice.get(field) is not None:
            try:
                parsed[field] = _decimal(invoice[field])
                if parsed[field] < 0:
                    findings.append(_finding("negative_amount", f"{field} is negative; verify credit-note intent", field=field))
            except ValueError:
                findings.append(_finding("invalid_numeric", f"{field} is not a finite monetary value", "error", field=field))
    discount = Decimal('0')
    if invoice.get('discount') is not None:
        try:
            discount = _decimal(invoice['discount'])
            if discount < 0:
                findings.append(_finding('negative_amount', 'Discount must not be negative', 'error', field='discount'))
        except ValueError:
            findings.append(_finding('invalid_numeric', 'Discount is not a finite monetary value', 'error', field='discount'))
    if all(field in parsed for field in MONEY_FIELDS):
        delta = parsed["subtotal"] + parsed["tax"] - discount - parsed["total"]
        if abs(delta) > Decimal("0.01"):
            findings.append(_finding("total_mismatch", "Subtotal plus tax minus explicit discount does not equal total (other charges are not inferred)", "error", difference=_money(delta), tolerance="0.01"))
    currency = invoice.get("currency")
    if currency is not None and not re.fullmatch(r"[A-Z]{3}", str(currency)):
        findings.append(_finding("invalid_currency", "Currency must be a three-letter uppercase code", "error"))
    invoice_date = invoice.get("invoice_date")
    if invoice_date:
        date_text = str(invoice_date).split("T")[0]
        valid = False
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
            try:
                datetime.strptime(date_text, fmt)
                valid = True
                break
            except ValueError:
                pass
        if not valid:
            findings.append(_finding("invalid_date", "Invoice date is not a recognized valid calendar date", "error"))
        elif re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", date_text):
            a, b, _ = map(int, date_text.split("/"))
            if a <= 12 and b <= 12 and a != b:
                findings.append(_finding("ambiguous_date", "Date could use day/month or month/day ordering"))
    items = invoice.get("line_items", [])
    if not isinstance(items, list):
        findings.append(_finding("invalid_line_items", "Line items must be a list", "error"))
        items = []
    line_totals = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            findings.append(_finding("invalid_line_item", f"Line {index + 1} is not an object", "error"))
            continue
        numbers = {}
        for field in ("quantity", "unit_price", "line_total"):
            if item.get(field) is not None:
                try:
                    numbers[field] = _decimal(item[field])
                except ValueError:
                    findings.append(_finding("invalid_numeric", f"Line {index + 1}: invalid {field}", "error", line=index + 1, field=field))
        if "line_total" in numbers:
            line_totals.append(numbers["line_total"])
        if len(numbers) == 3 and abs(numbers["quantity"] * numbers["unit_price"] - numbers["line_total"]) > Decimal("0.01"):
            findings.append(_finding("line_item_mismatch", f"Line {index + 1}: quantity times unit price differs from line total", "error", line=index + 1))
    if items and len(line_totals) == len(items) and "subtotal" in parsed and abs(sum(line_totals) - parsed["subtotal"]) > Decimal("0.01"):
        findings.append(_finding("line_items_subtotal_mismatch", "Line totals do not equal subtotal", "error"))
    return findings


def process_file(path: Path, filename: str) -> dict:
    """Ingest one file and return plain JSON-compatible pages/invoices/metadata."""
    sources = extract_sources(path, filename)
    invoices: list[dict] = []
    for source in sources:
        extracted = _tabular_invoices(source) if "rows" in source else _text_invoices(source)
        extracted = [ground_invoice(invoice, [source]) for invoice in extracted]
        # Merge only adjacent text pages bearing the same explicit invoice ID.
        # A numberless continuation is not guessed to belong to a prior invoice.
        for invoice in extracted:
            previous = invoices[-1] if invoices else None
            previous_page = previous.get("page_end", 0) if previous else 0
            invoice["page_start"] = invoice["page_end"] = source["page"]
            complete_repeat = bool(previous and all(previous.get(field) is not None and invoice.get(field) is not None for field in FIELDS))
            if ("rows" not in source and previous and not complete_repeat and previous.get("invoice_number") and invoice.get("invoice_number") == previous["invoice_number"] and previous_page + 1 == source["page"] and all(not previous.get(field) or not invoice.get(field) or _normalize(previous[field]) == _normalize(invoice[field]) for field in ("supplier_name", "customer_name", "invoice_date"))):
                for field in FIELDS:
                    if invoice[field] is not None:
                        _set_field(previous, field, str(invoice[field]), invoice["evidence"][field])
                previous["line_items"].extend(invoice["line_items"])
                if invoice["line_items"]:
                    previous["evidence"]["line_items"] = invoice["evidence"]["line_items"]
                previous["findings"].extend(invoice["findings"])
                previous["page_end"] = source["page"]
            else:
                invoices.append(invoice)
    failed_pages = sum(bool(source.get("error")) for source in sources)
    ocr_pages = {source["page"] for source in sources if source["method"] == "ocr"}
    for invoice in invoices:
        invoice["findings"].extend(validate_invoice(invoice))
        if not invoice["line_items"]:
            invoice["findings"].append(_finding("missing_line_items", "No reliably grounded line items were extracted"))
        if failed_pages:
            invoice["findings"].append(_finding("incomplete_document", "One or more document pages could not be read", "error"))
        if any(page in ocr_pages for page in range(invoice["page_start"], invoice["page_end"] + 1)):
            invoice["findings"].append(_finding("ocr_requires_review", "OCR text has no calibrated confidence; verify against the original scan"))
        invoice["needs_review"] = bool(invoice["findings"])
    # Duplicates within this document are visible, never silently discarded.
    identities: dict[tuple[str, str], list[dict]] = {}
    for invoice in invoices:
        if invoice.get("invoice_number") and invoice.get("supplier_name"):
            identity = (_normalize(invoice["supplier_name"]), _normalize(invoice["invoice_number"]))
            identities.setdefault(identity, []).append(invoice)
    for group in identities.values():
        if len(group) > 1:
            for invoice in group:
                invoice["findings"].append(_finding("duplicate_invoice", "Repeated supplier and invoice number in this document"))
                invoice["needs_review"] = True
    pages = [{key: value for key, value in source.items() if key != "rows"} for source in sources]
    status = "failed" if failed_pages == len(sources) else "partial" if failed_pages else "processed"
    return {"pages": pages, "invoices": invoices, "metadata": {"provider": "offline_deterministic", "extraction_provider": "offline_deterministic", "model": None, "prompt_version": None, "schema_version": SCHEMA_VERSION, "rule_version": RULE_VERSION, "filename": filename, "page_count": len(pages), "failed_pages": failed_pages, "status": status, "needs_review": bool(failed_pages or not invoices or any(invoice["needs_review"] for invoice in invoices)), "limitations": ["Label/header heuristic baseline; no LLM inference", "Formula cells require stored cached values", "Numberless continuation pages are not automatically assigned", "Cross-document duplicates must be checked by the persistence layer"]}}


def _unambiguous_date(value: Any) -> str | None:
    text = re.sub(r'\s+', '', str(value))
    dates = set()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%d%b%Y', '%d%B%Y', '%b%d,%Y', '%B%d,%Y'):
        try:
            dates.add(datetime.strptime(text, fmt).date().isoformat())
        except ValueError:
            pass
    return next(iter(dates)) if len(dates) == 1 else None


def _grounded(invoice: dict, field: str) -> bool:
    all_evidence = invoice.get("evidence", {})
    evidence = all_evidence.get(field) if isinstance(all_evidence, dict) else None
    if not isinstance(evidence, dict) or not evidence.get("source_text") or not any(evidence.get(key) is not None for key in ("page", "sheet", "row", "cell")):
        return False
    raw = str(evidence["source_text"])
    value = invoice.get(field)
    if value is None:
        return False
    if field == 'invoice_date':
        canonical = _unambiguous_date(raw)
        if canonical and canonical == _unambiguous_date(value):
            return True
    if field in MONEY_FIELDS:
        # Evidence can be a raw cell or an entire labelled line.
        try:
            return _decimal(raw) == _decimal(value)
        except ValueError:
            for token in re.findall(r"\(?[+-]?(?:\d{1,3}(?:[ ,.'  ]\d{3})+(?:[.,]\d{1,6})?|\d+(?:[.,]\d{1,6})?)\)?", raw):
                try:
                    if _decimal(token.strip()) == _decimal(value):
                        return True
                except ValueError:
                    pass
            return False
    return bool(re.search(r"(?<!\w)" + re.escape(_normalize(value)) + r"(?!\w)", _normalize(raw)))


def _grounded_items(invoice: dict) -> bool:
    """Require each compared table value to occur in its own source evidence."""
    table_evidence = invoice.get("evidence", {}).get("line_items")
    if not isinstance(table_evidence, dict) or not table_evidence.get("source_text"):
        return False
    for item in invoice.get("line_items", []):
        if not isinstance(item, dict):
            return False
        evidence = item.get("evidence", {})
        if not isinstance(evidence, dict):
            return False
        for field in ("description", "quantity", "unit_price", "line_total"):
            if item.get(field) is None:
                continue
            location = evidence.get(field) if isinstance(evidence.get(field), dict) else evidence
            numeric_field = "total" if field != "description" else "supplier_name"
            if not _grounded({numeric_field: item[field], "evidence": {numeric_field: location}}, numeric_field):
                return False
    return True


def _source_contains(location: dict, sources: list[dict]) -> bool:
    if not isinstance(location, dict) or not location.get("source_text"):
        return False
    def quote(value: Any) -> str:
        return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()
    snippet = quote(location["source_text"])
    for source in sources:
        if location.get("page") is not None and location["page"] != source.get("page"):
            continue
        if location.get("sheet") is not None and location["sheet"] != source.get("sheet"):
            continue
        text = source.get("text", "")
        if location.get("row") is not None and "rows" in source:
            if not isinstance(location["row"], int):
                continue
            # Adapter rows are ordered; logarithmic lookup avoids quadratic work
            # when validating every field of a large CSV/ledger.
            index = bisect.bisect_left(source["rows"], location["row"], key=lambda row: row["row"])
            row = source["rows"][index] if index < len(source["rows"]) else None
            if row is None or row["row"] != location["row"]:
                continue
            text = "\t".join(row["values"])
            if location.get("cell"):
                match = re.fullmatch(r"([A-Z]+)(\d+)", str(location["cell"]))
                if not match or int(match.group(2)) != row["row"]:
                    continue
                column = 0
                for letter in match.group(1):
                    column = column * 26 + ord(letter) - 64
                if not 0 < column <= len(row["values"]):
                    continue
                text = row["values"][column - 1]
        if snippet and snippet in quote(text):
            return True
    return False


def ground_invoice(invoice: dict, sources: list[dict]) -> dict:
    """Copy and fail closed on candidate values unsupported by actual sources.

    This is also available to an optional external extraction adapter: merely
    fabricating a convincing evidence quote does not establish grounding.
    """
    result = copy.deepcopy(invoice)
    result.setdefault("findings", [])
    evidence = result.get("evidence")
    if not isinstance(evidence, dict):
        evidence = result["evidence"] = {}
    for field in FIELDS:
        if result.get(field) is not None and (not _grounded(result, field) or not _source_contains(evidence.get(field), sources)):
            result[field] = None
            result["findings"].append(_finding("ungrounded_field", f"{field} is not supported by the referenced source", "error", field=field))
    for index, item in enumerate(result.get("line_items", [])):
        if not isinstance(item, dict):
            continue  # validate_invoice reports the malformed item separately.
        item_evidence = item.get("evidence", {})
        if not isinstance(item_evidence, dict):
            item_evidence = {}
        for field in ("description", "quantity", "unit_price", "line_total"):
            if item.get(field) is None:
                continue
            location = item_evidence.get(field) if isinstance(item_evidence.get(field), dict) else item_evidence
            comparable_field = "total" if field != "description" else "supplier_name"
            candidate = {comparable_field: item[field], "evidence": {comparable_field: location}}
            if not _grounded(candidate, comparable_field) or not _source_contains(location, sources):
                item[field] = None
                result["findings"].append(_finding("ungrounded_line_item", f"Line {index + 1}: {field} is not supported by source", "error", field=field, line=index + 1))
    result["needs_review"] = bool(result["findings"])
    return result


def reconcile(left: dict, right: dict, tolerance: str = "0.01") -> dict:
    """Evidence-gated pairwise comparison (not a many-document match selector).

    'matched' requires grounded identity, currency, and every scalar amount.
    Missing or ungrounded comparisons cannot masquerade as successful matches.
    """
    try:
        allowed = _decimal(tolerance)
        if allowed < 0:
            raise ValueError("negative tolerance")
    except ValueError as exc:
        raise ValueError("Tolerance must be a finite non-negative decimal") from exc
    differences, missing, ungrounded = [], [], []
    comparisons = []
    for field in FIELDS:
        a, b = left.get(field), right.get(field)
        if a is None or b is None or a == "" or b == "":
            missing.append(field)
            continue
        if not _grounded(left, field) or not _grounded(right, field):
            ungrounded.append(field)
            continue
        comparison = {"field": field, "left": a, "right": b, "left_evidence": left["evidence"][field], "right_evidence": right["evidence"][field]}
        if field in MONEY_FIELDS:
            try:
                delta = _decimal(a) - _decimal(b)
                equal = abs(delta) <= allowed
                comparison["difference"] = _money(delta)
            except ValueError:
                ungrounded.append(field)
                continue
        else:
            equal = _normalize(a) == _normalize(b)
        comparison["matches"] = equal
        comparisons.append(comparison)
        if not equal:
            differences.append(comparison)
    # Compare extracted item rows when both sides actually supply them. A missing
    # table is recorded as incomplete rather than treated as an empty true table.
    left_items, right_items = left.get("line_items", []), right.get("line_items", [])
    if bool(left_items) != bool(right_items):
        missing.append("line_items")
    elif left_items and right_items:
        if not _grounded_items(left) or not _grounded_items(right):
            ungrounded.append("line_items")
        else:
            equal = len(left_items) == len(right_items)
            if equal:
                for a, b in zip(left_items, right_items):
                    for field in ("description", "quantity", "unit_price", "line_total"):
                        if a.get(field) is None or b.get(field) is None:
                            if a.get(field) != b.get(field):
                                equal = False
                        elif field == "description":
                            equal = equal and _normalize(a[field]) == _normalize(b[field])
                        else:
                            try:
                                equal = equal and abs(_decimal(a[field]) - _decimal(b[field])) <= allowed
                            except ValueError:
                                equal = False
            if not equal:
                differences.append({"field": "line_items", "left": left_items, "right": right_items, "left_evidence": left["evidence"]["line_items"], "right_evidence": right["evidence"]["line_items"], "matches": False})
    identity_fields = ("invoice_number", "supplier_name", "currency")
    identity_ready = all(field not in missing + ungrounded for field in identity_fields)
    conflicts = bool(left.get("conflicts") or right.get("conflicts"))
    if conflicts:
        status = "ambiguous"
    elif not identity_ready:
        status = "insufficient_evidence"
    elif differences:
        status = "mismatch"
    elif missing or ungrounded:
        status = "insufficient_evidence"
    else:
        status = "matched"
    return {"status": status, "differences": differences, "comparisons": comparisons, "missing_fields": missing, "ungrounded_fields": ungrounded, "tolerance": _money(allowed), "rule_version": RULE_VERSION, "needs_review": status != "matched"}
