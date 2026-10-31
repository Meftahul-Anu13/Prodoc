"""Offline native-reader tests and EXPLICITLY MOCKED MinerU SDK contract tests.

No live MinerU runtime, model download, OCR accuracy or cloud API is exercised.
Spreadsheets/images/PDF inputs are real files, synthesized locally.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import types
import zipfile
from pathlib import Path

import pytest

from backend.app import sources


def read(tmp_path, content: bytes, filename: str):
    path = tmp_path / "b709f0f3b00d5a0c"  # No extension: production hashed storage.
    path.write_bytes(content)
    return sources.read_sources(path, filename, tmp_path / "artifacts")


def failure(code, operation):
    with pytest.raises(sources.SourceError) as captured:
        operation()
    assert captured.value.code == code
    assert str(captured.value).startswith(code + ":")
    return captured.value


def test_csv_irregular_grid_has_no_header_or_business_assumptions(tmp_path):
    result = read(tmp_path, b'Heading\n"Supplier","Example, Ltd"\n"","  ","9007199254740993.1200"\n\n"tail"\n', "../../original.CSV")
    assert [block["text"] for block in result["blocks"]] == ["Heading", "Supplier", "Example, Ltd", "  ", "9007199254740993.1200", "tail"]
    assert [block["location"]["cell"] for block in result["blocks"]] == ["A1", "A2", "B2", "B3", "C3", "A5"]
    assert result["metadata"]["logical_rows"] == 5
    assert result["metadata"]["delimiter"] == ","
    assert "invoices" not in result
    assert json.loads((tmp_path / "artifacts" / "source.json").read_text(encoding="utf-8")) == result
    original = tmp_path / "artifacts" / result["metadata"]["original_artifact"]
    assert original.name == "original.csv"
    assert original.read_bytes().startswith(b"Heading")
    assert not (tmp_path / "original.CSV").exists()


@pytest.mark.parametrize("delimiter", [";", "\t", "|"])
def test_irregular_noncomma_csv_and_multiline_cells(tmp_path, delimiter):
    text = f'Cover\nleft{delimiter}right\nx{delimiter}"line one\nline two"{delimiter}tail\n'
    result = read(tmp_path, text.encode(), "source.csv")
    assert result["metadata"]["delimiter"] == delimiter
    assert [block["text"] for block in result["blocks"]] == ["Cover", "left", "right", "x", "line one\nline two", "tail"]
    assert result["blocks"][-1]["location"]["cell"] == "C3"


def test_utf16_csv_preserves_every_unicode_character(tmp_path):
    result = read(tmp_path, "label,é€中文\nlong,0000123\n".encode("utf-16"), "source.csv")
    assert result["metadata"]["encoding"] == "utf-16"
    assert result["blocks"][1]["text"] == "é€中文"
    assert result["blocks"][-1]["text"] == "0000123"


def test_csv_ambiguous_delimiters_and_nul_rejected(tmp_path):
    failure("CSV_DELIMITER", lambda: read(tmp_path, b"heading\na,b;c\nx,y;z;w\n", "source.csv"))
    failure("CSV_ENCODING", lambda: read(tmp_path, b"a,\x00b", "source.csv"))


def test_csv_malformed_quotes_not_silently_repaired(tmp_path):
    failure("CORRUPT_CSV", lambda: read(tmp_path, b'"unterminated\n', "source.csv"))
    assert (tmp_path / "artifacts" / "source_error.json").is_file()
    assert not (tmp_path / "artifacts" / "source.json").exists()


@pytest.mark.parametrize("limit,value,code", [("MAX_CELLS", 2, "SHEET_LIMIT"), ("MAX_ROWS", 1, "SHEET_LIMIT"), ("MAX_TEXT_CHARS", 3, "TEXT_LIMIT")])
def test_csv_limits_reject_not_truncate(tmp_path, monkeypatch, limit, value, code):
    monkeypatch.setattr(sources, limit, value)
    failure(code, lambda: read(tmp_path, b"a,b\nc,d\n", "source.csv"))
    assert not (tmp_path / "artifacts" / "source.json").exists()


def xlsx_bytes(tmp_path, *, patch=None):
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Visible"
    sheet["A1"] = "Cover title"
    sheet["C2"] = 0.1
    sheet["C2"].number_format = "0.000000000000000000"
    sheet["A4"] = "001234"
    sheet["D5"] = "=SUM(C2,1)"
    sheet["F8"] = "merger anchor"
    sheet.merge_cells("F8:G9")
    sheet.row_dimensions[4].hidden = True
    sheet.column_dimensions["C"].hidden = True
    hidden = workbook.create_sheet("Hidden")
    hidden.sheet_state = "veryHidden"
    hidden["B3"] = "hidden retained"
    sheet["H10"] = False
    sheet["I11"] = "  "
    path = tmp_path / "fixture.xlsx"
    workbook.save(path)
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    xml = members["xl/worksheets/sheet1.xml"].decode()
    xml = xml.replace("<v>0.1</v>", "<v>9007199254740993.123456789012345678900</v>")
    # openpyxl uses either <v/> (lxml installed) or <v></v> (stdlib
    # serializer). Patch the actual formula cell, not one serialization spelling.
    xml, replacements = re.subn(r'(<c\b[^>]*\br="D5"[^>]*>).*?(</c>)',
                               r'\g<1><f>SUM(C2,1)</f><v>9007199254740994.123456789012345678900</v>\g<2>', xml)
    assert replacements == 1, "Fixture must contain exactly one D5 formula cell"
    members["xl/worksheets/sheet1.xml"] = xml.encode()
    if patch:
        patch(members)
    output = __import__("io").BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_real_xlsx_retains_ooxml_precision_formula_cache_hidden_and_merged(tmp_path):
    result = read(tmp_path, xlsx_bytes(tmp_path), "financial.xlsx")
    blocks = {f'{block["location"]["sheet"]}!{block["location"]["cell"]}': block for block in result["blocks"]}
    assert blocks["Visible!C2"]["text"] == "9007199254740993.123456789012345678900"
    location = blocks["Visible!C2"]["location"]
    assert location["raw_value"] == blocks["Visible!C2"]["text"]
    assert location["number_format"] == "0.000000000000000000"
    formula = blocks["Visible!D5"]
    assert "9007199254740994.123456789012345678900" in formula["text"]
    assert formula["location"]["formula"] == "SUM(C2,1)"
    assert formula["location"]["cache_freshness"] == "unknown"
    assert formula["location"]["formula_unverified"] is True
    assert blocks["Visible!A4"]["text"] == "001234"
    assert blocks["Visible!H10"]["text"] == "0"
    assert blocks["Visible!I11"]["text"] == "  "
    assert blocks["Hidden!B3"]["location"]["hidden_sheet"] is True
    assert result["metadata"]["sheets"][0]["merged_ranges"] == ["F8:G9"]
    assert result["metadata"]["sheets"][0]["hidden_rows"] == [4]
    assert result["metadata"]["sheets"][0]["hidden_columns"] == [{"min": 3, "max": 3}]
    assert result["metadata"]["sheets"][1]["state"] == "veryHidden"
    assert len(result["pages"]) == 2
    assert result["warnings"] and result["metadata"]["needs_review"]


def test_xlsx_no_cache_and_shared_formula_uncertainty_are_preserved(tmp_path):
    def patch(members):
        xml = members["xl/worksheets/sheet1.xml"].decode()
        xml = xml.replace("<f>SUM(C2,1)</f><v>9007199254740994.123456789012345678900</v>", '<f t="shared" si="0"/>')
        members["xl/worksheets/sheet1.xml"] = xml.encode()
    result = read(tmp_path, xlsx_bytes(tmp_path, patch=patch), "book.xlsx")
    formula = next(block for block in result["blocks"] if block["location"]["cell"] == "D5")
    assert "[missing]" in formula["text"]
    assert formula["location"]["formula_attributes"] == {"t": "shared", "si": "0"}
    assert formula["location"]["cached_value"] is None


def test_xlsx_shared_strings_rich_text_and_merged_interior_not_dropped(tmp_path):
    def patch(members):
        xml = members["xl/worksheets/sheet1.xml"].decode()
        xml = xml.replace('<c r="A1" t="inlineStr"><is><t>Cover title</t></is></c>', '<c r="A1" t="s"><v>0</v></c>')
        # This malformed-but-recoverable merged interior must not be erased by
        # openpyxl's merged-cell object replacement.
        xml = xml.replace('</sheetData>', '<row r="9"><c r="G9" t="inlineStr"><is><t>interior data</t></is></c></row></sheetData>')
        members["xl/worksheets/sheet1.xml"] = xml.encode()
        members["xl/sharedStrings.xml"] = (f'<sst xmlns="{sources.S[1:-1]}"><si><r><t>Rich </t></r><r><t>text</t></r></si></sst>').encode()
    result = read(tmp_path, xlsx_bytes(tmp_path, patch=patch), "book.xlsx")
    assert result["blocks"][0]["text"] == "Rich text"
    assert any(block["location"]["cell"] == "G9" and block["text"] == "interior data" for block in result["blocks"])


def test_xlsx_sparse_coordinates_not_rectangular_expansion(tmp_path):
    def patch(members):
        members["xl/worksheets/sheet1.xml"] = members["xl/worksheets/sheet1.xml"].replace(b'r="I11"', b'r="XFD99999"')
    result = read(tmp_path, xlsx_bytes(tmp_path, patch=patch), "book.xlsx")
    assert any(block["location"]["cell"] == "XFD99999" for block in result["blocks"])
    assert result["metadata"]["cell_count"] < 20


def test_xlsx_external_relation_and_unknown_cell_type_fail_explicitly(tmp_path):
    def external(members):
        members["xl/_rels/workbook.xml.rels"] = members["xl/_rels/workbook.xml.rels"].replace(b'Target="/xl/worksheets/sheet1.xml"', b'Target="https://invalid.test/book" TargetMode="External"')
    failure("XLSX_SCHEMA", lambda: read(tmp_path, xlsx_bytes(tmp_path, patch=external), "book.xlsx"))
    def unknown(members):
        members["xl/worksheets/sheet1.xml"] = members["xl/worksheets/sheet1.xml"].replace(b't="inlineStr"', b't="futureType"', 1)
    failure("XLSX_SCHEMA", lambda: read(tmp_path, xlsx_bytes(tmp_path, patch=unknown), "book.xlsx"))


def test_xlsx_zip_budget_and_cell_budget_fail(tmp_path, monkeypatch):
    content = xlsx_bytes(tmp_path)
    monkeypatch.setattr(sources, "MAX_ZIP_BYTES", 10)
    failure("ZIP_LIMIT", lambda: read(tmp_path, content, "book.xlsx"))
    monkeypatch.setattr(sources, "MAX_ZIP_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(sources, "MAX_CELLS", 2)
    failure("SHEET_LIMIT", lambda: read(tmp_path, content, "book.xlsx"))


def test_real_legacy_xls_preserves_cells_hidden_merges_and_precision_warning(tmp_path):
    import xlwt
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("Legacy")
    sheet.write(0, 0, "Top title")
    sheet.write(2, 3, 123.45678901234567)
    sheet.write(4, 2, xlwt.Formula("1+2"))
    sheet.write_merge(6, 7, 1, 2, "merged text")
    sheet.row(2).hidden = True
    hidden = workbook.add_sheet("Hidden")
    hidden.visibility = 1
    hidden.write(3, 4, "secret retained")
    path = tmp_path / "book.xls"
    workbook.save(str(path))
    result = read(tmp_path, path.read_bytes(), "book.xls")
    assert [page["sheet"] for page in result["pages"]] == ["Legacy", "Hidden"]
    numeric = next(block for block in result["blocks"] if block["location"]["cell"] == "D3")
    assert numeric["text"] == str(123.45678901234567)
    assert numeric["location"]["binary_numeric"]
    assert all(block["location"]["formula_cache_uncertain"] for block in result["blocks"])
    formula = next(block for block in result["blocks"] if block["location"]["cell"] == "C5")
    assert formula["location"]["raw_value"] == ""
    assert "EMPTY_XLS_VALUE" in formula["text"]
    assert any(block["text"] == "secret retained" and block["location"]["hidden_sheet"] for block in result["blocks"])
    assert result["metadata"]["sheets"][0]["merged_ranges"] == ["B7:C8"]
    assert any("binary" in warning and "formula" in warning for warning in result["warnings"])


# Deliberately not a live MinerU test. The small fake models only the documented
# SDK methods and fixture schema; it cannot prove a GPU/ONNX/model deployment.
class MockMinerUResult:
    def __init__(self, payload):
        self.payload = copy.deepcopy(payload)

    def to_dict(self):
        return copy.deepcopy(self.payload)

    @classmethod
    def from_dict(cls, payload):
        assert payload["schema"] == "docvortex.middle"
        assert payload["schema_version"] == "2.0"
        for page in payload["pages"]:
            assert isinstance(page["blocks"], list)
            for block in page["blocks"]:
                assert block["type"] in {"text", "header"}
        return cls(payload)

    def markdown(self, *, add_markers=False):
        # Assert production asks for FULL rendering: headers must not disappear.
        assert add_markers is True
        return "\n".join(span["content"] for page in self.payload["pages"] for block in page["blocks"] for span in block["content"])

    def save(self, writer):
        writer.write_string("middle_json.json", json.dumps(self.payload))
        writer.write_string("markdown.md", "original full-document markdown")
        writer.write_string("structured_content.json", "{}")
        writer.write_string("model_output.json", '{"original_model_output":true}')
        writer.write("images/figure.png", b"original parser asset bytes")


def mineru_payload(count):
    return {"schema": "docvortex.middle", "schema_version": "2.0", "is_full_document": True,
            "metadata": {"file_suffix": "pdf", "producer": {"name": "mineru", "version": sources.MINERU_VERSION}},
            "pages": [{"page_idx": index, "blocks": [{"type": "text", "index": 0, "bbox": [1, 2, 100, 30],
                       "content": [{"type": "text", "content": f"Mock page {index + 1} text"}]}]} for index in range(count)]}


@pytest.fixture
def mock_mineru(tmp_path, monkeypatch):
    """OFFLINE MOCK ONLY: synthetic SDK, no mineru distribution imported."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    env = {"MINERU_TIER": "basic", "MINERU_MODEL_SOURCE": "local", "MINERU_MODEL_BASE_DIR": str(model_dir),
           "MINERU_MODEL_SMALL_BACKEND": "onnx", "MINERU_MODEL_VLM_SERVER_URL": ""}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = types.SimpleNamespace(model=types.SimpleNamespace(source="local", base_dir=str(model_dir), small_backend="onnx",
                                   vlm=types.SimpleNamespace(server_url="", engine="llama-cpp")))
    calls = []
    state = {"payload": mineru_payload(1)}
    def parse(path, *, tier, ocr_mode, page_range, image_analysis):
        assert Path(path).suffix in {".pdf", ".png"}  # SDK cannot route hashed paths.
        assert tier == "basic" and ocr_mode == "auto" and page_range == "" and image_analysis is True
        calls.append(Path(path))
        if "exception" in state:
            raise state["exception"]
        return MockMinerUResult(state["payload"])
    module = types.ModuleType("mineru")
    parser = types.ModuleType("mineru.parser")
    parser.parse, parser.ParseResult = parse, MockMinerUResult
    configuration = types.ModuleType("mineru.config")
    configuration.config = config
    monkeypatch.setitem(sys.modules, "mineru", module)
    monkeypatch.setitem(sys.modules, "mineru.parser", parser)
    monkeypatch.setitem(sys.modules, "mineru.config", configuration)
    actual_version = sources.packages.version
    monkeypatch.setattr(sources.packages, "version", lambda name: {"mineru": sources.MINERU_VERSION, "docvortex": sources.DOCVORTEX_VERSION}.get(name) or actual_version(name))
    return state, calls, config


def pdf_bytes(count):
    import fitz
    with fitz.open() as document:
        for index in range(count):
            page = document.new_page()
            page.insert_text((40, 40), f"Real PDF input page {index + 1}")
        return document.tobytes()


def test_mock_mineru_real_11_page_pdf_full_coverage_artifacts_and_no_fake_geometry(tmp_path, mock_mineru):
    state, calls, _ = mock_mineru
    state["payload"] = mineru_payload(11)
    result = read(tmp_path, pdf_bytes(11), "original.pdf")
    assert len(calls) == 1
    assert len(result["pages"]) == 11
    assert result["blocks"][-1]["location"]["page"] == 11
    assert result["metadata"]["coverage"] == "complete"
    assert result["metadata"]["finalization_blocked"] is False
    assert result["metadata"]["expected_pages"] == 11
    assert all("bbox" not in block["location"] for block in result["blocks"])
    directory = calls[0].parent / "parser-000001"
    assert json.loads((directory / "parser_return.json").read_text())["pages"] == state["payload"]["pages"]
    assert (directory / "middle_json.json").exists()
    assert (directory / "model_output.json").exists()
    assert (directory / "page-000011.md").read_text() == "Mock page 11 text"
    manifest = json.loads((directory / "artifact_manifest.json").read_text())
    assert (directory / manifest["images/figure.png"]).read_bytes() == b"original parser asset bytes"
    assert json.loads((tmp_path / "artifacts" / "source.json").read_text())["metadata"]["page_count"] == 11


@pytest.mark.parametrize("mutation", ["ten-pages", "range-flag", "duplicate-index", "out-of-order", "wrong-index"])
def test_mock_mineru_partial_coverage_is_failure_not_ten_page_success(tmp_path, mock_mineru, mutation):
    state, calls, _ = mock_mineru
    state["payload"] = mineru_payload(11)
    if mutation == "ten-pages":
        state["payload"]["pages"].pop()
    elif mutation == "range-flag":
        state["payload"]["is_full_document"] = False
    elif mutation == "duplicate-index":
        state["payload"]["pages"][-1]["page_idx"] = 9
    elif mutation == "out-of-order":
        state["payload"]["pages"].reverse()
    else:
        state["payload"]["pages"][-1]["page_idx"] = 12
    failure("PARTIAL_COVERAGE", lambda: read(tmp_path, pdf_bytes(11), "source.pdf"))
    assert (calls[0].parent / "parser-000001" / "middle_json.json").exists()
    assert not (tmp_path / "artifacts" / "source.json").exists()
    error = json.loads((tmp_path / "artifacts" / "source_error.json").read_text())
    assert error["metadata"]["finalization_blocked"] is True


def test_mock_mineru_unknown_schema_and_runtime_failure_have_explicit_codes(tmp_path, mock_mineru):
    state, _, _ = mock_mineru
    state["payload"]["schema_version"] = "future"
    failure("MINERU_SCHEMA", lambda: read(tmp_path, pdf_bytes(1), "source.pdf"))
    state["exception"] = RuntimeError("local model missing")
    failure("MINERU_FAILED", lambda: read(tmp_path, pdf_bytes(1), "source.pdf"))


def test_mock_mineru_empty_page_prevents_finalization_and_retains_other_pages(tmp_path, mock_mineru):
    state, _, _ = mock_mineru
    state["payload"] = mineru_payload(2)
    state["payload"]["pages"][1]["blocks"] = []
    result = read(tmp_path, pdf_bytes(2), "source.pdf")
    assert len(result["pages"]) == 2
    assert result["pages"][1]["status"] == "needs_review"
    assert result["metadata"]["coverage"] == "review"
    assert result["metadata"]["finalization_blocked"] is True


def test_mock_mineru_full_rendering_keeps_headers(tmp_path, mock_mineru):
    state, _, _ = mock_mineru
    state["payload"]["pages"][0]["blocks"].append({"type": "header", "index": 1, "bbox": [1, 1, 9, 9],
                                                  "content": [{"type": "text", "content": "retained header"}]})
    result = read(tmp_path, pdf_bytes(1), "source.pdf")
    assert any(block["text"] == "retained header" for block in result["blocks"])


def test_mock_mineru_tiff_all_frames_are_normalized_and_each_checked(tmp_path, mock_mineru):
    from PIL import Image
    frame1, frame2 = Image.new("RGB", (12, 20), "red"), Image.new("RGB", (16, 8), "blue")
    image_path = tmp_path / "frames.tiff"
    frame1.save(image_path, save_all=True, append_images=[frame2])
    result = read(tmp_path, image_path.read_bytes(), "source.tiff")
    _, calls, _ = mock_mineru
    assert [path.name for path in calls] == ["frame-000001.png", "frame-000002.png"]
    assert [Image.open(path).size for path in calls] == [(12, 20), (16, 8)]
    assert [block["location"]["page"] for block in result["blocks"]] == [1, 2]
    assert result["metadata"]["inspected_page_count"] == 2


def test_mock_mineru_jpeg_exif_orientation_is_applied_before_sdk(tmp_path, mock_mineru):
    from PIL import Image
    image = Image.new("RGB", (12, 20), "red")
    exif = Image.Exif()
    exif[274] = 6
    image_path = tmp_path / "rotated.jpg"
    image.save(image_path, exif=exif)
    read(tmp_path, image_path.read_bytes(), "source.jpeg")
    with Image.open(mock_mineru[1][0]) as normalized:
        assert normalized.size == (20, 12)
        assert normalized.mode == "RGB"
        assert normalized.getexif().get(274) is None


@pytest.mark.parametrize("env,value", [("MINERU_TIER", ""), ("MINERU_MODEL_SOURCE", "huggingface"), ("MINERU_MODEL_SMALL_BACKEND", "auto"), ("MINERU_MODEL_VLM_SERVER_URL", "http://127.0.0.1:9000")])
def test_mock_mineru_explicit_local_only_config_required(tmp_path, mock_mineru, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    failure("MINERU_CONFIG", lambda: read(tmp_path, pdf_bytes(1), "source.pdf"))
    assert mock_mineru[1] == []


def test_mock_mineru_import_cached_remote_config_cannot_bypass_environment(tmp_path, mock_mineru):
    mock_mineru[2].model.vlm.server_url = "https://remote.invalid"
    failure("MINERU_CONFIG", lambda: read(tmp_path, pdf_bytes(1), "source.pdf"))
    assert mock_mineru[1] == []


def test_mock_mineru_version_mismatch_and_missing_install_are_not_fallbacks(tmp_path, mock_mineru, monkeypatch):
    monkeypatch.setattr(sources.packages, "version", lambda name: "0.0")
    failure("MINERU_VERSION", lambda: read(tmp_path, pdf_bytes(1), "source.pdf"))
    def missing(name):
        raise sources.packages.PackageNotFoundError(name)
    monkeypatch.setattr(sources.packages, "version", missing)
    failure("MINERU_UNAVAILABLE", lambda: read(tmp_path, pdf_bytes(1), "source.pdf"))
    assert mock_mineru[1] == []


def test_image_pixel_and_frame_budgets_precede_any_sdk_call(tmp_path, mock_mineru, monkeypatch):
    from PIL import Image
    image_path = tmp_path / "frames.tif"
    image = Image.new("RGB", (10, 10))
    image.save(image_path, save_all=True, append_images=[image])
    data = image_path.read_bytes()
    monkeypatch.setattr(sources, "MAX_IMAGE_PIXELS", 99)
    failure("IMAGE_LIMIT", lambda: read(tmp_path, data, "source.tif"))
    monkeypatch.setattr(sources, "MAX_IMAGE_PIXELS", 100)
    monkeypatch.setattr(sources, "MAX_TOTAL_IMAGE_PIXELS", 199)
    failure("IMAGE_LIMIT", lambda: read(tmp_path, data, "source.tif"))
    monkeypatch.setattr(sources, "MAX_TOTAL_IMAGE_PIXELS", 200)
    monkeypatch.setattr(sources, "MAX_PAGES", 1)
    failure("PAGE_LIMIT", lambda: read(tmp_path, data, "source.tif"))
    assert mock_mineru[1] == []


def test_pdf_page_count_limit_and_encryption_precede_sdk(tmp_path, mock_mineru, monkeypatch):
    monkeypatch.setattr(sources, "MAX_PAGES", 1)
    failure("PAGE_LIMIT", lambda: read(tmp_path, pdf_bytes(2), "source.pdf"))
    import fitz
    with fitz.open() as document:
        document.new_page()
        data = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="reader")
    failure("ENCRYPTED_PDF", lambda: read(tmp_path, data, "source.pdf"))
    assert mock_mineru[1] == []


def test_parser_artifact_filenames_are_internally_generated(tmp_path):
    writer = sources._ArtifactWriter(tmp_path)
    writer.write("../../outside.txt", b"safe")
    filename = writer.manifest["../../outside.txt"]
    assert filename.startswith("asset-")
    assert (tmp_path / filename).read_bytes() == b"safe"
    assert not (tmp_path.parent / "outside.txt").exists()
    failure("MINERU_SCHEMA", lambda: writer.write("../../outside.txt", b"overwritten"))


def test_source_validation_and_empty_input(tmp_path):
    missing = tmp_path / "absent"
    failure("FILE_MISSING", lambda: sources.read_sources(missing, "a.csv", tmp_path / "artifacts"))
    failure("UNSUPPORTED_FORMAT", lambda: sources.read_sources(missing, "a.exe", tmp_path / "artifacts"))
    failure("FILE_LIMIT", lambda: read(tmp_path, b"", "a.csv"))
    failure("EMPTY_SOURCE", lambda: read(tmp_path, b",\n,\n", "a.csv"))


def test_pdf_text_layer_supplements_numbers_missing_from_parser_output(tmp_path, mock_mineru):
    import fitz
    state, _, _ = mock_mineru
    state["payload"]["pages"][0]["blocks"][0]["content"][0]["content"] = "Widget 50.00 100.00"
    with fitz.open() as document:
        page = document.new_page()
        for x, text in ((40, "Widget"), (200, "2"), (260, "50.00"), (330, "100.00")):
            page.insert_text((x, 60), text)
        data = document.tobytes()
    result = read(tmp_path, data, "original.pdf")
    extra = [b for b in result["blocks"] if b["location"].get("kind") == "pdf_text_layer_supplement"]
    assert [b["text"] for b in extra] == ["Widget 2 50.00 100.00"]
    assert extra[0]["location"]["missing_from_parser"] == ["2"]
    assert result["metadata"]["text_layer_supplement_rows"] == 1
    assert any("text-layer" in w for w in result["warnings"])
