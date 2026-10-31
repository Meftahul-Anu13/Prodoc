"""Canonical, non-semantic local readers. No sandbox, business rules or LLM calls.

MinerU's *released* 4.0.12 SDK and DocVortex 0.6.7 schema were inspected,
not inferred from the old CLI: https://opendatalab.github.io/MinerU/usage/sdk_api/
https://pypi.org/project/mineru/4.0.12/ and https://pypi.org/project/docvortex/0.6.7/
The optional parser profile must pin both distributions. Tests mock this SDK;
they are not evidence of live model/runtime accuracy. Provision models offline.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import posixpath
import re
import shutil
import tempfile
import warnings as pywarnings
import zipfile
from importlib import metadata as packages
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_ZIP_BYTES = 128 * 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000
MAX_ROWS = 100_000
MAX_CELLS = 1_000_000
MAX_TEXT_CHARS = 20_000_000
MAX_PAGES = 1500
MAX_IMAGE_PIXELS = 40_000_000
MAX_TOTAL_IMAGE_PIXELS = 500_000_000
MINERU_VERSION = "4.0.12"
DOCVORTEX_VERSION = "0.6.7"
SUPPORTED = {".csv", ".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class SourceError(ValueError):
    """Controlled ingestion failure; callers should persist the stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _json(path: Path, value: Any) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    if len(data.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise SourceError("ARTIFACT_LIMIT", "Serialized source/parser artifact exceeds output budget")
    # Never expose an incomplete source.json to the next processing stage.
    fd, name = tempfile.mkstemp(prefix=".source-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _col(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _coordinate(value: str) -> tuple[int, int]:
    import re
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", value)
    if not match:
        raise SourceError("XLSX_SCHEMA", "Invalid cell coordinate")
    column = 0
    for character in match[1]:
        column = column * 26 + ord(character) - 64
    row = int(match[2])
    if row > MAX_ROWS or column > 16384:
        raise SourceError("SHEET_LIMIT", "Cell coordinate exceeds supported bounds")
    return row, column


class _Bundle:
    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self.pages: list[dict] = []
        self.warnings: list[str] = []
        self.metadata: dict[str, Any] = {}
        self.cells = 0
        self.rows = 0
        self.characters = 0

    def count(self, cells: int = 0, rows: int = 0) -> None:
        self.cells += cells
        self.rows += rows
        if self.cells > MAX_CELLS or self.rows > MAX_ROWS:
            raise SourceError("SHEET_LIMIT", "Input exceeds row/cell limits; nothing was truncated")

    def add(self, text: str, **location: Any) -> None:
        # Whitespace is data in a cell; do not strip or normalize its value.
        if text == "":
            return
        self.characters += len(text)
        if len(self.blocks) >= MAX_CELLS or self.characters > MAX_TEXT_CHARS:
            raise SourceError("TEXT_LIMIT", "Canonical source exceeds text/block limits")
        self.blocks.append({"id": f"b{len(self.blocks) + 1:07d}", "text": text, "location": location})

    def sheet(self, index: int, name: str, method: str, start: int, **extra: Any) -> None:
        text = "\n".join(block["text"] for block in self.blocks[start:])
        self.pages.append({"page": index, "sheet": name, "method": method, "text": text,
                           "status": "processed" if text else "empty", **extra})


def _decode(data: bytes) -> tuple[str, str, bool]:
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16", errors="strict"), "utf-16", False
        return data.decode("utf-8-sig", errors="strict"), "utf-8-sig", False
    except UnicodeDecodeError:
        from charset_normalizer import from_bytes
        match = from_bytes(data).best()
        if match is None:
            raise SourceError("CSV_ENCODING", "Cannot determine lossless CSV encoding")
        return data.decode(match.encoding, errors="strict"), match.encoding, True


def _csv(path: Path, bundle: _Bundle) -> None:
    try:
        text, encoding, inferred = _decode(path.read_bytes())
        if "\x00" in text:
            raise SourceError("CSV_ENCODING", "NUL characters in decoded CSV")
        if len(text) > MAX_TEXT_CHARS:
            raise SourceError("TEXT_LIMIT", "CSV text exceeds limit")
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            scores = []
            # Quote-aware full-file candidates retain irregular rows and multiline cells.
            for candidate in (",", ";", "\t", "|"):
                try:
                    score = 0
                    for index, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=candidate, strict=True), 1):
                        if index > MAX_ROWS:
                            raise SourceError("SHEET_LIMIT", "CSV exceeds logical row limit")
                        score += len(row) > 1
                    scores.append((score, candidate))
                except csv.Error:
                    continue
            scores.sort(reverse=True)
            if not scores:
                raise SourceError("CORRUPT_CSV", "No valid quote-aware delimiter parse")
            if scores[0][0] and len(scores) > 1 and scores[1][0] >= scores[0][0] * 0.75:
                raise SourceError("CSV_DELIMITER", "Ambiguous CSV delimiter; re-export with an unambiguous delimiter")
            delimiter = scores[0][1] if scores[0][0] else ","
            bundle.warnings.append(f"Irregular CSV: delimiter inferred as {delimiter!r}; review original.")
        for row_number, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True), 1):
            bundle.count(cells=len(row), rows=1)
            for column, value in enumerate(row, 1):
                bundle.add(value, page=1, sheet="CSV", row=row_number, column=column,
                           cell=f"{_col(column)}{row_number}", kind="cell")
        bundle.sheet(1, "CSV", "csv", 0, encoding=encoding)
        bundle.metadata.update(reader="stdlib.csv", encoding=encoding, delimiter=delimiter, logical_rows=bundle.rows)
        if inferred:
            bundle.warnings.append(f"CSV encoding inferred as {encoding}; verify original accented text.")
    except SourceError:
        raise
    except (csv.Error, UnicodeError, LookupError) as exc:
        raise SourceError("CORRUPT_CSV", "CSV decoding/syntax or field-size limit failure") from exc


def _xlsx(path: Path, bundle: _Bundle) -> None:
    from defusedxml import ElementTree as ET
    from openpyxl.styles.numbers import BUILTIN_FORMATS

    def string(element: Any) -> str:
        if element is None:
            return ""
        # Rich text runs, without conflating phonetic annotations with cell text.
        return "".join(node.text or "" for child in element
                       for node in ([child] if child.tag == S + "t" else child.findall(S + "t") if child.tag == S + "r" else []))

    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > MAX_ZIP_ENTRIES or sum(entry.file_size for entry in entries) > MAX_ZIP_BYTES:
                raise SourceError("ZIP_LIMIT", "XLSX expanded bytes/member count exceeds limit")
            if len(names) != len(set(names)):
                raise SourceError("XLSX_SCHEMA", "Duplicate ZIP members are ambiguous")
            if any(entry.file_size > 1024 * 1024 and entry.file_size > max(1, entry.compress_size) * 250 for entry in entries):
                raise SourceError("ZIP_LIMIT", "Suspicious XLSX expansion ratio")
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise SourceError("XLSX_SCHEMA", "Macro-containing workbook is unsupported")
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            if workbook.tag != S + "workbook":
                raise SourceError("XLSX_SCHEMA", "Unsupported workbook namespace")
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {item.attrib["Id"]: item for item in relationships}
            shared = []
            if "xl/sharedStrings.xml" in names:
                shared = [string(item) for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall(S + "si")]
            formats: dict[int, str] = dict(BUILTIN_FORMATS)
            styles = []
            if "xl/styles.xml" in names:
                root = ET.fromstring(archive.read("xl/styles.xml"))
                formats.update({int(item.attrib["numFmtId"]): item.attrib["formatCode"] for item in root.findall(f"{S}numFmts/{S}numFmt")})
                styles = [formats.get(int(item.attrib.get("numFmtId", "0")), "General") for item in root.findall(f"{S}cellXfs/{S}xf")]
            sheets = workbook.findall(f"{S}sheets/{S}sheet")
            if not 0 < len(sheets) <= MAX_PAGES:
                raise SourceError("PAGE_LIMIT", "Invalid workbook sheet count")
            sheet_metadata = []
            formula_count = 0
            for page, sheet in enumerate(sheets, 1):
                relationship = targets[sheet.attrib[REL]]
                if relationship.attrib.get("TargetMode") == "External" or not relationship.attrib.get("Type", "").endswith("/worksheet"):
                    raise SourceError("XLSX_SCHEMA", "Only internal worksheets are supported")
                target = relationship.attrib["Target"]
                member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
                if not member.startswith("xl/") or "\\" in member or ".." in member.split("/"):
                    raise SourceError("XLSX_SCHEMA", "Unsafe worksheet relationship")
                root = ET.fromstring(archive.read(member))
                if root.tag != S + "worksheet":
                    raise SourceError("XLSX_SCHEMA", "Unsupported worksheet schema")
                name, state = sheet.attrib["name"], sheet.attrib.get("state", "visible")
                merged = [item.attrib["ref"] for item in root.findall(f"{S}mergeCells/{S}mergeCell")]
                hidden_rows = [int(item.attrib["r"]) for item in root.findall(f"{S}sheetData/{S}row") if item.attrib.get("hidden") in {"1", "true"}]
                hidden_columns = [{"min": int(item.attrib["min"]), "max": int(item.attrib["max"])} for item in root.findall(f"{S}cols/{S}col") if item.attrib.get("hidden") in {"1", "true"}]
                sheet_metadata.append({"name": name, "state": state, "merged_ranges": merged,
                                       "hidden_rows": hidden_rows, "hidden_columns": hidden_columns})
                start, seen = len(bundle.blocks), set()
                for row in root.findall(f"{S}sheetData/{S}row"):
                    bundle.count(rows=1)
                    for cell in row.findall(S + "c"):
                        bundle.count(cells=1)
                        coordinate = cell.attrib["r"]
                        row_number, column = _coordinate(coordinate)
                        if coordinate in seen:
                            raise SourceError("XLSX_SCHEMA", "Duplicate worksheet cell coordinate")
                        seen.add(coordinate)
                        value_node, formula = cell.find(S + "v"), cell.find(S + "f")
                        raw = value_node.text if value_node is not None and value_node.text is not None else ""
                        cell_type = cell.attrib.get("t", "n")
                        if cell_type == "s":
                            number = int(raw)
                            if not 0 <= number < len(shared):
                                raise SourceError("XLSX_SCHEMA", "Invalid shared-string reference")
                            value = shared[number]
                        elif cell_type == "inlineStr":
                            value = string(cell.find(S + "is"))
                        elif cell_type in {"n", "str", "b", "d", "e"}:
                            value = raw
                        else:
                            raise SourceError("XLSX_SCHEMA", "Unknown cell type")
                        extra: dict[str, Any] = {"ooxml_type": cell_type}
                        if value_node is not None:
                            extra["raw_value"] = raw
                        if "s" in cell.attrib:
                            style = int(cell.attrib["s"])
                            if not 0 <= style < len(styles):
                                raise SourceError("XLSX_SCHEMA", "Invalid cell style reference")
                            extra["number_format"] = styles[style]
                        if formula is not None:
                            formula_count += 1
                            extra.update(formula=formula.text or "", formula_attributes=dict(formula.attrib),
                                         formula_unverified=True, cached_value=value if value_node is not None else None,
                                         cache_freshness="unknown")
                            value = f"FORMULA={formula.text or '[shared/array formula]'}; CACHED_VALUE={value if value_node is not None else '[missing]'}; FRESHNESS=UNKNOWN"
                        bundle.add(value, page=page, sheet=name, row=row_number, column=column,
                                   cell=coordinate, kind="cell", hidden_sheet=state != "visible", **extra)
                bundle.sheet(page, name, "spreadsheet", start, hidden=state != "visible", merged_ranges=merged)
            if formula_count:
                bundle.warnings.append(f"{formula_count} formula cell(s): formulas were not executed; cached values have unknown freshness.")
            bundle.metadata.update(reader="native-OOXML", sheets=sheet_metadata, formula_cells=formula_count,
                                   date_system=workbook.find(S + "workbookPr").attrib.get("date1904", "0") if workbook.find(S + "workbookPr") is not None else "0")
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("CORRUPT_XLSX", "Unable to read bounded OOXML workbook") from exc


def _xls(path: Path, bundle: _Bundle) -> None:
    try:
        import xlrd
    except ImportError as exc:
        raise SourceError("XLS_UNAVAILABLE", "Install xlrd==2.0.1 for legacy spreadsheets") from exc
    try:
        workbook = xlrd.open_workbook(str(path), on_demand=True, formatting_info=True)
        try:
            if not 0 < workbook.nsheets <= MAX_PAGES:
                raise SourceError("PAGE_LIMIT", "Invalid legacy workbook sheet count")
            sheets = []
            for page in range(1, workbook.nsheets + 1):
                sheet = workbook.sheet_by_index(page - 1)
                if sheet.nrows > MAX_ROWS or sheet.nrows * sheet.ncols > MAX_CELLS:
                    raise SourceError("SHEET_LIMIT", "Legacy sheet dimensions exceed limits")
                merged = [f"{_col(c0 + 1)}{r0 + 1}:{_col(c1)}{r1}" for r0, r1, c0, c1 in sheet.merged_cells]
                hidden_rows = [index + 1 for index, info in sheet.rowinfo_map.items() if info.hidden]
                hidden_columns = [index + 1 for index, info in sheet.colinfo_map.items() if info.hidden]
                sheets.append({"name": sheet.name, "hidden": bool(sheet.visibility), "merged_ranges": merged,
                               "hidden_rows": hidden_rows, "hidden_columns": hidden_columns})
                start = len(bundle.blocks)
                for row in range(sheet.nrows):
                    bundle.count(cells=sheet.ncols, rows=1)
                    for column in range(sheet.ncols):
                        cell = sheet.cell(row, column)
                        if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
                            continue
                        # Do not invent a decimal/date lexical representation from BIFF doubles.
                        raw = str(cell.value)
                        value = xlrd.error_text_from_code.get(cell.value, raw) if cell.ctype == xlrd.XL_CELL_ERROR else raw
                        if value == "":
                            # xlrd represents an uncached formula as empty TEXT,
                            # indistinguishable from explicitly stored empty text.
                            # Preserve that occupied cell rather than erase it.
                            value = "[EMPTY_XLS_VALUE: stored text or formula cache; provenance unknown]"
                        xf = workbook.xf_list[sheet.cell_xf_index(row, column)]
                        number_format = workbook.format_map.get(xf.format_key)
                        bundle.add(value, page=page, sheet=sheet.name, row=row + 1, column=column + 1,
                                   cell=f"{_col(column + 1)}{row + 1}", kind="cell", hidden_sheet=bool(sheet.visibility),
                                   raw_value=raw, number_format=number_format.format_str if number_format else None,
                                   xls_cell_type=cell.ctype, binary_numeric=cell.ctype in {xlrd.XL_CELL_NUMBER, xlrd.XL_CELL_DATE},
                                   formula_cache_uncertain=True)
                bundle.sheet(page, sheet.name, "spreadsheet", start, hidden=bool(sheet.visibility), merged_ranges=merged)
            bundle.metadata.update(reader="xlrd", sheets=sheets, date_system=workbook.datemode)
            bundle.warnings.append("XLS stores binary numbers/date serials; original decimal tokens cannot be recovered. xlrd exposes formula caches, not reliable formula provenance; every legacy cell requires formula/cache review.")
        finally:
            workbook.release_resources()
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("CORRUPT_XLS", "Unable to read legacy workbook") from exc


def _pixels(width: int, height: int) -> int:
    count = width * height
    if width <= 0 or height <= 0 or count > MAX_IMAGE_PIXELS:
        raise SourceError("IMAGE_LIMIT", "Image/frame exceeds pixel limit")
    return count


def _inspect_pdf(path: Path) -> int:
    import fitz
    try:
        with fitz.open(path) as document:
            if document.needs_pass:
                raise SourceError("ENCRYPTED_PDF", "Password-protected PDF is unsupported")
            if not 0 < document.page_count <= MAX_PAGES:
                raise SourceError("PAGE_LIMIT", "Invalid PDF page count")
            # Reject pathological physical dimensions before a parser can rasterize them.
            for page in document:
                if any(not math.isfinite(value) or value <= 0 or value > 14400 for value in (page.rect.width, page.rect.height)):
                    raise SourceError("IMAGE_LIMIT", "PDF page dimensions exceed bounds")
            return document.page_count
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("CORRUPT_PDF", "Cannot inspect all PDF pages") from exc


def _normalize_images(path: Path, output: Path) -> list[Path]:
    from PIL import Image, ImageOps
    paths = []
    total = 0
    try:
        with pywarnings.catch_warnings():
            pywarnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                count = getattr(image, "n_frames", 1)
                if not 0 < count <= MAX_PAGES:
                    raise SourceError("PAGE_LIMIT", "Image frame count exceeds limit")
                for index in range(count):
                    image.seek(index)
                    total += _pixels(*image.size)
                    if total > MAX_TOTAL_IMAGE_PIXELS:
                        raise SourceError("IMAGE_LIMIT", "Total image pixels exceed document budget")
                    with ImageOps.exif_transpose(image) as oriented:
                        with oriented.convert("RGB") as normalized:
                            target = output / f"frame-{index + 1:06d}.png"
                            normalized.save(target, format="PNG")
                            paths.append(target)
        return paths
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("CORRUPT_IMAGE", "Invalid, oversized or unreadable image/frame") from exc


class _ArtifactWriter:
    """SDK DataWriter duck type; never trust paths emitted by the parser.

    Fixed protocol outputs retain their names; every other name is hashed. The
    manifest preserves the mapping, so original JSON asset references remain
    auditable without allowing parser-controlled output paths or overwrites.
    """
    def __init__(self, directory: Path):
        self.directory = directory
        self.manifest: dict[str, str] = {}
        self.bytes = 0

    def write(self, path: str, data: bytes) -> None:
        if not isinstance(path, str) or not isinstance(data, bytes):
            raise SourceError("MINERU_SCHEMA", "Invalid parser artifact")
        self.bytes += len(data)
        if len(self.manifest) >= MAX_ZIP_ENTRIES or self.bytes > MAX_ARTIFACT_BYTES:
            raise SourceError("ARTIFACT_LIMIT", "Parser artifacts exceed output budget")
        fixed = {"markdown.md", "middle_json.json", "structured_content.json", "model_output.json"}
        name = path if path in fixed else "asset-" + hashlib.sha256(path.encode("utf-8")).hexdigest() + ".bin"
        if path in self.manifest:
            raise SourceError("MINERU_SCHEMA", "Duplicate parser artifact")
        (self.directory / name).write_bytes(data)
        self.manifest[path] = name

    def write_string(self, path: str, data: str) -> None:
        self.write(path, data.encode("utf-8"))


def _mineru_runtime() -> Any:
    tier = os.getenv("MINERU_TIER", "").strip().lower()
    if tier not in {"flash", "basic", "standard", "advanced"}:
        raise SourceError("MINERU_CONFIG", "Select MINERU_TIER explicitly: flash, basic, standard or advanced")
    root = os.getenv("MINERU_MODEL_BASE_DIR", "")
    backend = os.getenv("MINERU_MODEL_SMALL_BACKEND", "")
    if os.getenv("MINERU_MODEL_SOURCE") != "local" or not root or not Path(root).is_dir() or backend not in {"onnx", "torch"}:
        raise SourceError("MINERU_CONFIG", "Require local model source, existing MINERU_MODEL_BASE_DIR and explicit onnx/torch small backend")
    if os.getenv("MINERU_MODEL_VLM_SERVER_URL", "").strip():
        raise SourceError("MINERU_CONFIG", "Remote VLM endpoints are forbidden, including loopback services")
    if tier in {"standard", "advanced"} and os.getenv("MINERU_MODEL_VLM_ENGINE", "") not in {"llama-cpp", "vllm", "lmdeploy", "mlx"}:
        raise SourceError("MINERU_CONFIG", "Higher tiers require an explicit local MINERU_MODEL_VLM_ENGINE")
    try:
        if packages.version("mineru") != MINERU_VERSION or packages.version("docvortex") != DOCVORTEX_VERSION:
            raise SourceError("MINERU_VERSION", f"Require mineru=={MINERU_VERSION} and docvortex=={DOCVORTEX_VERSION}")
        from mineru.config import config
        from mineru.parser import ParseResult, parse
    except ImportError as exc:
        raise SourceError("MINERU_UNAVAILABLE", "Install the separate pinned local MinerU dependency profile") from exc
    except packages.PackageNotFoundError as exc:
        raise SourceError("MINERU_UNAVAILABLE", "Pinned local MinerU profile is not installed") from exc
    # SDK config is loaded once on import. Check effective values as well as env:
    # changing env after a previous import must not re-enable cloud/model downloads.
    model = config.model
    if (model.source != "local" or model.vlm.server_url or model.small_backend != backend
            or Path(model.base_dir).expanduser().resolve() != Path(root).expanduser().resolve()):
        raise SourceError("MINERU_CONFIG", "Effective SDK configuration is not the requested local-only profile")
    if tier in {"standard", "advanced"} and model.vlm.engine != os.getenv("MINERU_MODEL_VLM_ENGINE"):
        raise SourceError("MINERU_CONFIG", "Effective VLM engine differs from explicit selection")
    return parse, ParseResult, tier


_NUMBER = re.compile(r"(?<![\w.,])-?\d[\d.,]*")


def _text_layer_supplement(path: Path, bundle: _Bundle) -> None:
    """Add visual rows from a PDF's embedded text layer whose numbers MinerU omitted.

    Cross-check only: rows are extra evidence blocks, never replacements. Scans and
    images have no text layer and are unaffected.
    """
    import fitz
    added = 0
    try:
        with fitz.open(path) as document:
            for number, page in enumerate(document, 1):
                parsed = next((p["text"] for p in bundle.pages if p["page"] == number), "")
                known = set(_NUMBER.findall(parsed))
                rows: dict[int, list[tuple]] = {}
                for word in page.get_text("words"):
                    rows.setdefault(round((word[1] + word[3]) / 2 / 3), []).append(word)
                for key in sorted(rows):
                    words = sorted(rows[key], key=lambda w: w[0])
                    text = " ".join(w[4] for w in words)
                    missing = [t for t in _NUMBER.findall(text) if t not in known]
                    if missing:
                        bundle.add(text, page=number, kind="pdf_text_layer_supplement",
                                   missing_from_parser=missing, geometry="not_attested")
                        added += 1
    except Exception:
        bundle.warnings.append("PDF text-layer cross-check could not run; parser output is unsupplemented.")
        return
    bundle.metadata["text_layer_supplement_rows"] = added
    if added:
        bundle.warnings.append(f"{added} PDF text-layer row(s) contain numbers absent from MinerU output and were added as supplemental evidence; verify against the original.")


def _text_layer_rows(page) -> list[str]:
    """Visual rows from a PDF text layer; wide gaps become ' | ' so table columns survive."""
    rows: dict[int, list] = {}
    for word in page.get_text("words"):
        rows.setdefault(round((word[1] + word[3]) / 2 / 3), []).append(word)
    lines = []
    for key in sorted(rows):
        words = sorted(rows[key], key=lambda w: w[0])
        text = words[0][4]
        for before, word in zip(words, words[1:]):
            text += (" | " if word[0] - before[2] > 20 else " ") + word[4]
        lines.append(text)
    return lines


def _pdf_text_layer(path: Path, bundle: _Bundle, expected: int) -> bool:
    """Digital PDFs: read the embedded text layer directly (seconds, not OCR minutes).

    Used only when EVERY page has a real text layer; any scanned or image-only
    page sends the whole document to MinerU OCR, so nothing is silently skipped.
    """
    if os.getenv("PDF_TEXT_LAYER", "auto").strip().lower() == "off":
        return False
    import fitz
    with fitz.open(path) as document:
        pages = []
        for page in document:
            rows = _text_layer_rows(page)
            chars = sum(len(r) for r in rows)
            # Short text on an image-bearing page is likely a scan with a stamp/footer.
            if chars < 10 or (chars < 80 and page.get_images()):
                return False
            pages.append(rows)
    if len(pages) != expected:
        return False
    for number, rows in enumerate(pages, 1):
        start = len(bundle.blocks)
        for line_number, row in enumerate(rows, 1):
            bundle.add(row, page=number, line=line_number, kind="pdf_text_layer")
        text = "\n".join(b["text"] for b in bundle.blocks[start:])
        bundle.pages.append({"page": number, "text": text, "method": "pdf_text_layer", "status": "processed"})
    bundle.metadata.update(reader="pymupdf-text-layer", expected_pages=expected, inspected_page_count=expected,
                           coverage="complete", finalization_blocked=False)
    return True


def _document(path: Path, suffix: str, output: Path, bundle: _Bundle) -> None:
    # Inspect/normalize every frame before invoking any model.
    if suffix == ".pdf":
        expected = _inspect_pdf(path)
        if _pdf_text_layer(path, bundle, expected):
            return
        inputs = [(path, expected, 0)]
    else:
        normalized = _normalize_images(path, output)
        expected = len(normalized)
        inputs = [(frame, 1, index) for index, frame in enumerate(normalized)]
    parse, result_type, tier = _mineru_runtime()
    bundle.metadata.update(reader="mineru", version=MINERU_VERSION, docvortex_version=DOCVORTEX_VERSION,
                           tier=tier, expected_pages=expected, inspected_page_count=expected,
                           normalization="original-pdf" if suffix == ".pdf" else "EXIF-transposed-RGB-PNG-per-frame")
    for index, (input_path, count, offset) in enumerate(inputs, 1):
        directory = output / f"parser-{index:06d}"
        directory.mkdir()
        writer = _ArtifactWriter(directory)
        try:
            # Empty page_range is explicitly ALL pages in the pinned SDK.
            result = parse(input_path, tier=tier, ocr_mode="auto", page_range="", image_analysis=True)
            payload = result.to_dict()
            _json(directory / "parser_return.json", payload)
            # Save the SDK's original middle JSON, model output, markdown and assets.
            result.save(writer)
            _json(directory / "artifact_manifest.json", writer.manifest)
            if (not isinstance(payload, dict) or payload.get("schema") != "docvortex.middle"
                    or payload.get("schema_version") != "2.0"):
                raise SourceError("MINERU_SCHEMA", "Unknown MinerU/DocVortex result schema")
            parsed_pages = payload.get("pages")
            if (not isinstance(parsed_pages, list)
                    or any(not isinstance(page, dict) or type(page.get("page_idx")) is not int for page in parsed_pages)):
                raise SourceError("MINERU_SCHEMA", "Missing or malformed parser page list/indices")
            indices = [page["page_idx"] for page in parsed_pages]
            if payload.get("is_full_document") is not True or indices != list(range(count)):
                raise SourceError("PARTIAL_COVERAGE", f"Expected all {count} page(s); returned page indices do not cover the inspected input")
            # Validate the *whole* payload through the pinned SDK's schema, not an
            # ad-hoc text search that might silently discard unknown block types.
            try:
                validated = result_type.from_dict(payload).to_dict()
            except (ValueError, TypeError, KeyError) as exc:
                raise SourceError("MINERU_SCHEMA", "Parser output failed the pinned SDK schema validation") from exc
            for page in validated["pages"]:
                page_number = offset + page["page_idx"] + 1
                # Rendering each validated page retains page provenance without
                # inventing boxes or mapping full-document Markdown to page 1.
                single = {**validated, "pages": [page], "is_full_document": False}
                # FULL mode (selected by add_markers) includes auxiliary content
                # such as headers/footers that DEFAULT rendering may suppress.
                text = result_type.from_dict(single).markdown(add_markers=True)
                if not isinstance(text, str):
                    raise SourceError("MINERU_SCHEMA", "Page renderer did not return Markdown text")
                markdown_name = f"page-{page_number:06d}.md"
                if len(text) > MAX_TEXT_CHARS:
                    raise SourceError("TEXT_LIMIT", "Parser page exceeds text limit")
                (directory / markdown_name).write_text(text, encoding="utf-8")
                artifact = f"{output.name}/{directory.name}/{markdown_name}"
                for line_number, line in enumerate(text.splitlines(), 1):
                    if line.strip():
                        bundle.add(line, page=page_number, line=line_number, kind="derived_markdown",
                                   artifact=artifact, parser_page_idx=page["page_idx"], geometry="not_attested")
                empty = not text.strip()
                bundle.pages.append({"page": page_number, "text": text, "method": "mineru",
                                     "status": "needs_review" if empty else "processed",
                                     "provenance": "page-rendered parser Markdown; verify against original",
                                     "artifact": artifact, **({"error": "EMPTY_PARSER_PAGE: verify blank page versus failed extraction"} if empty else {})})
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError("MINERU_FAILED", "Local parser/render/schema operation failed; retained artifacts require review") from exc
    if suffix == ".pdf":
        _text_layer_supplement(path, bundle)
    empty = any(page.get("error") for page in bundle.pages)
    bundle.metadata.update(coverage="review" if empty else "complete", finalization_blocked=empty)
    bundle.warnings.append("MinerU text is derived parser Markdown, not calibrated OCR truth. Geometry is deliberately not fabricated; compare retained parser artifacts and original pages.")


def read_sources(path: Path | str, filename: str, artifact_dir: Path | str) -> dict:
    """Read every source cell/page and persist source.json *before* any LLM use.

    Routing uses the original filename suffix, never the hashed storage path.
    ``artifact_dir`` must be an application-generated per-job directory. The
    filename is display metadata only and is never used as an output path.
    Fatal schema/coverage/resource failures raise SourceError (with .code),
    retaining parser output and source_error.json rather than returning success.
    """
    path, artifact_dir = Path(path), Path(artifact_dir)
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise SourceError("UNSUPPORTED_FORMAT", "Use PDF, PNG, JPEG, TIFF, CSV, XLSX or XLS")
    if not path.is_file():
        raise SourceError("FILE_MISSING", "Source file does not exist")
    if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
        raise SourceError("FILE_LIMIT", "Source is empty or exceeds 100 MiB")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="source-", dir=artifact_dir))
    # Safe, internally generated suffix also fixes the SDK's own suffix routing.
    original = output / ("original" + suffix)
    shutil.copyfile(path, original)
    bundle = _Bundle()
    try:
        if suffix == ".csv":
            _csv(original, bundle)
        elif suffix == ".xlsx":
            _xlsx(original, bundle)
        elif suffix == ".xls":
            _xls(original, bundle)
        else:
            _document(original, suffix, output, bundle)
        if not bundle.blocks:
            raise SourceError("EMPTY_SOURCE", "No nonempty canonical source blocks recovered")
        review = bool(bundle.warnings or any(page.get("error") for page in bundle.pages))
        bundle.metadata.setdefault("coverage", "complete")
        bundle.metadata.setdefault("finalization_blocked", False)
        bundle.metadata.update(filename=filename, page_count=len(bundle.pages), needs_review=review,
                               status="needs_review" if review else "processed", cell_count=bundle.cells,
                               original_artifact=f"{output.name}/{original.name}")
        result = {"schema": "canonical-source-v1", "blocks": bundle.blocks, "pages": bundle.pages,
                  "warnings": bundle.warnings, "metadata": bundle.metadata}
        _json(artifact_dir / "source.json", result)
        return result
    except SourceError as exc:
        _json(artifact_dir / "source_error.json", {"code": exc.code, "message": str(exc),
               "metadata": {**bundle.metadata, "coverage": "failed", "finalization_blocked": True},
               "artifact_directory": output.name})
        raise
