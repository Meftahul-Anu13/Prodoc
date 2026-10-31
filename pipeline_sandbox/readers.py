"""Read source material without assuming a header row or one invoice per row."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import posixpath
import zipfile
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10_000
MAX_CELLS = 100_000
MAX_ZIP_BYTES = 100 * 1024 * 1024


class LabError(ValueError):
    pass


def json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def _string(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return "" if value is None else str(value)


def _col(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _decode(data: bytes, encoding: str | None) -> tuple[str, str]:
    if encoding:
        return data.decode(encoding, errors="strict"), encoding
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="strict"), "utf-16"
    try:
        return data.decode("utf-8-sig", errors="strict"), "utf-8-sig"
    except UnicodeDecodeError:
        from charset_normalizer import from_bytes
        match = from_bytes(data).best()
        if match is None:
            raise LabError("Unknown encoding; supply --encoding. No bytes were discarded.")
        return data.decode(match.encoding, errors="strict"), match.encoding


def read_source(path: Path, output: Path, parser: str = "auto", *,
                encoding: str | None = None, delimiter: str | None = None,
                mineru_tier: str | None = None, artifacts_path: str | None = None) -> dict:
    path = path.resolve()
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise LabError("Input must be a nonempty local file of at most 10 MiB.")
    suffix = path.suffix.lower()
    blocks: list[dict] = []
    warnings: list[str] = []
    metadata: dict[str, Any] = {}

    def add(text: str, **location: Any) -> None:
        if len(blocks) >= MAX_CELLS:
            raise LabError("Source limit exceeded. Nothing is silently truncated.")
        if text.strip():
            blocks.append({"id": f"b{len(blocks) + 1:06d}", "text": text, "location": location})

    # Native reading preserves the entire sparse grid. It does NOT interpret it.
    if suffix == ".csv":
        text, detected = _decode(path.read_bytes(), encoding)
        if "\x00" in text:
            raise LabError("NUL characters in decoded CSV; check its encoding.")
        metadata["encoding"] = detected
        if delimiter:
            if len(delimiter) != 1:
                raise LabError("--delimiter must be one character.")
            reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
            metadata["delimiter"] = delimiter
        else:
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
                metadata["delimiter"] = dialect.delimiter
            except csv.Error:
                # Sniffer often rejects irregular key/value exports. Compare quote-aware
                # candidate parses without assuming equal-width rows or a header.
                scores = []
                for separator in (",", ";", "\t", "|"):
                    try:
                        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=separator, strict=True))
                    except csv.Error:
                        continue
                    scores.append((sum(len(row) > 1 for row in rows), separator))
                scores.sort(reverse=True)
                if not scores:
                    raise LabError("CSV syntax invalid for tested delimiters; specify --delimiter or repair the original.")
                best = scores[0]
                if best[0] and len(scores) > 1 and scores[1][0] >= best[0] * 0.75:
                    raise LabError("Ambiguous CSV delimiter; specify --delimiter. No candidate was silently chosen.")
                selected = best[1] if best[0] else ","
                metadata["delimiter"] = selected
                warnings.append(f"Irregular rows: delimiter inferred as {selected!r}; verify or override --delimiter.")
                dialect = type("LabDialect", (csv.excel,), {"delimiter": selected})
            reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
        cells = 0
        for row_no, row in enumerate(reader, 1):
            cells += len(row)
            if row_no > MAX_ROWS or cells > MAX_CELLS:
                raise LabError("CSV row/cell limit exceeded; split the file explicitly.")
            for column, value in enumerate(row, 1):
                add(value, sheet="CSV", row=row_no, cell=f"{_col(column)}{row_no}", kind="cell")
        metadata.update(reader="stdlib.csv", logical_rows=row_no if 'row_no' in locals() else 0)
        if detected not in {"utf-8-sig", "utf-16"} and not encoding:
            warnings.append(f"Encoding inferred as {detected}; verify accented text or specify --encoding.")
    elif suffix == ".xlsx":
        _read_xlsx(path, add, metadata, warnings)
    elif suffix == ".xls":
        _read_xls(path, add, metadata, warnings)
    elif suffix in {".txt", ".md"}:
        text, detected = _decode(path.read_bytes(), encoding)
        for line_no, line in enumerate(text.splitlines(), 1):
            add(line, line=line_no, kind="text", artifact="original")
        metadata.update(reader="plain-text", encoding=detected)
    else:
        selected = "docling" if parser == "auto" else parser
        markdown, info = _parse_document(path, output, selected, mineru_tier, artifacts_path)
        metadata.update(info)
        for line_no, line in enumerate(markdown.splitlines(), 1):
            add(line, line=line_no, kind="derived_markdown", artifact="parsed.md", page=None)
        warnings.append("Document evidence references retained parser Markdown, NOT original page/cell coordinates. OCR errors can still be grounded to parser text. Compare the original visually.")
    if not blocks:
        raise LabError("No readable source blocks. Empty extraction is not success.")
    bundle = {
        "schema": "disposable-source-v1", "original_name": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "metadata": metadata, "warnings": warnings, "blocks": blocks,
        "interpretation": "No header or invoice-row assumptions have been applied.",
    }
    json_write(output / "source.json", bundle)
    return bundle


def _read_xlsx(path: Path, add: Any, metadata: dict, warnings: list[str]) -> None:
    from defusedxml import ElementTree as ET
    from openpyxl import load_workbook
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rel_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 5000 or sum(e.file_size for e in entries) > MAX_ZIP_BYTES:
            raise LabError("Workbook archive size/entry limit exceeded.")
        if any(e.file_size > max(1, e.compress_size) * 200 for e in entries):
            raise LabError("Workbook expansion-ratio limit exceeded.")
        if any(e.filename.lower().endswith("vbaproject.bin") for e in entries):
            raise LabError("Macro-containing workbook is outside this experiment.")
        workbook_xml = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib.get("Target", "") for r in rels
                   if r.attrib.get("TargetMode") != "External"}
        raw: dict[str, dict[str, dict]] = {}
        count = 0
        for sheet in workbook_xml.findall("s:sheets/s:sheet", ns):
            target = targets.get(sheet.attrib[rel_attr], "")
            member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
            if not member.startswith("xl/") or ".." in member.split("/"):
                raise LabError("Unexpected workbook relationship path.")
            root = ET.fromstring(archive.read(member))
            cells = {}
            for cell in root.findall(".//s:sheetData/s:row/s:c", ns):
                count += 1
                if count > MAX_CELLS:
                    raise LabError("Workbook cell limit exceeded.")
                value, formula = cell.find("s:v", ns), cell.find("s:f", ns)
                cells[cell.attrib["r"]] = {
                    "lexeme": value.text if value is not None else None,
                    "type": cell.attrib.get("t", "n"),
                    "formula": formula.text if formula is not None else None,
                    "has_formula": formula is not None,
                }
            raw[sheet.attrib["name"]] = cells
        # No calculate/save operations, external-link refresh or formula execution.
        workbook = load_workbook(path, read_only=False, data_only=False, keep_links=False)
        try:
            sheets = []
            for sheet in workbook.worksheets:
                if sheet.max_row > MAX_ROWS or sheet.max_row * sheet.max_column > MAX_CELLS:
                    raise LabError("Workbook dimensions exceed experiment limits.")
                sheets.append({"name": sheet.title, "state": sheet.sheet_state,
                               "merged_ranges": [str(r) for r in sheet.merged_cells.ranges]})
                for row in sheet.iter_rows():
                    for cell in row:
                        info = raw.get(sheet.title, {}).get(cell.coordinate, {})
                        value = cell.value
                        extra: dict[str, Any] = {}
                        if info.get("has_formula"):
                            value = f"FORMULA={cell.value}; CACHED_VALUE={info.get('lexeme')}; FRESHNESS=UNKNOWN"
                            extra["formula_unverified"] = True
                            warnings.append(f"Unverified formula/cache at {sheet.title}!{cell.coordinate}.")
                        elif info.get("type") == "n" and info.get("lexeme") is not None:
                            # Retain the stored numeric token, not str(openpyxl_float).
                            value = info["lexeme"]
                            extra["number_format"] = cell.number_format
                        add(_string(value), sheet=sheet.title, row=cell.row, cell=cell.coordinate,
                            kind="cell", hidden_sheet=sheet.sheet_state != "visible", **extra)
            metadata.update(reader="openpyxl+original-OOXML-lexemes", version=version("openpyxl"), sheets=sheets)
        finally:
            workbook.close()


def _read_xls(path: Path, add: Any, metadata: dict, warnings: list[str]) -> None:
    import xlrd
    warnings.append("Legacy XLS numeric cells are stored as binary values. Their original decimal precision cannot be reconstructed; inspect monetary values and formula caches manually.")
    workbook = xlrd.open_workbook(str(path), on_demand=True)
    count = 0
    try:
        sheets = []
        for sheet in workbook.sheets():
            if sheet.nrows > MAX_ROWS:
                raise LabError("XLS row limit exceeded.")
            sheets.append({"name": sheet.name, "hidden": bool(sheet.visibility)})
            for row in range(sheet.nrows):
                for column in range(sheet.ncols):
                    count += 1
                    if count > MAX_CELLS:
                        raise LabError("XLS cell limit exceeded.")
                    cell = sheet.cell(row, column)
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        value = xlrd.xldate_as_datetime(value, workbook.datemode).isoformat()
                    add(_string(value), sheet=sheet.name, row=row + 1, cell=f"{_col(column + 1)}{row + 1}",
                        kind="cell", hidden_sheet=bool(sheet.visibility),
                        binary_numeric=cell.ctype == xlrd.XL_CELL_NUMBER)
        metadata.update(reader="xlrd", version=version("xlrd"), sheets=sheets)
    finally:
        workbook.release_resources()


def _parse_document(path: Path, output: Path, parser: str,
                    mineru_tier: str | None, artifacts_path: str | None) -> tuple[str, dict]:
    if parser == "docling":
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise LabError("Docling is not installed. Use the separate requirements-docling.txt environment, or select MinerU.") from exc
        options = PdfPipelineOptions(enable_remote_services=False)
        if artifacts_path:
            options.artifacts_path = Path(artifacts_path).resolve()
        converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
        result = converter.convert(path, max_num_pages=50, max_file_size=MAX_BYTES)
        status = str(result.status).lower()
        if "success" not in status or "partial" in status:
            raise LabError(f"Docling did not complete: {result.status}")
        doc = result.document
        markdown = doc.export_to_markdown()
        (output / "parser_document.json").write_text(doc.model_dump_json(indent=2), encoding="utf-8")
        info = {"reader": "docling", "version": version("docling"), "status": str(result.status)}
    elif parser == "mineru":
        if not mineru_tier:
            raise LabError("Select --mineru-tier explicitly for your installed release; this lab never guesses its OCR/backend profile.")
        try:
            from mineru.parser import parse
        except ImportError as exc:
            raise LabError("MinerU SDK unavailable. Install a verified release in a separate environment; see README. No cloud fallback.") from exc
        result = parse(str(path), tier=mineru_tier)
        markdown = result.markdown()
        json_write(output / "parser_document.json", result.to_dict())
        info = {"reader": "mineru", "version": version("mineru"), "tier": mineru_tier,
                "coverage": "SDK invocation only; independently verify page coverage in retained output."}
    else:
        raise LabError("Use --parser docling or --parser mineru for PDF/image/Office conversion.")
    (output / "parsed.md").write_text(markdown, encoding="utf-8")
    return markdown, info
