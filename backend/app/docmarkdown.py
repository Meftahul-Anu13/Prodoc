"""Readable Markdown export of the canonical source (document.md).

Every format (PDF text layer, MinerU OCR, images, CSV/XLS/XLSX) becomes one
Markdown file with page/sheet headings. Spreadsheet sheets are rebuilt as
tables from cell coordinates. Each line keeps an HTML-comment anchor with its
source block ID, so text in the Markdown can be traced back to the evidence
used for grounding. It is a derived view; source.json remains the evidence.
"""

MAX_TABLE_COLUMNS = 40


def _cell(text):
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _sheet_table(blocks):
    rows, columns = {}, set()
    for block in blocks:
        location = block["location"]
        rows.setdefault(location["row"], {})[location["column"]] = block
        columns.add(location["column"])
    ordered = sorted(columns)[:MAX_TABLE_COLUMNS]
    lines = ["| row | " + " | ".join(f"col {c}" for c in ordered) + " |",
             "|---|" + "---|" * len(ordered)]
    for row in sorted(rows):
        cells = [_cell(rows[row][c]["text"]) if c in rows[row] else "" for c in ordered]
        ids = ",".join(rows[row][c]["id"] for c in ordered if c in rows[row])
        lines.append(f"| {row} | " + " | ".join(cells) + f" | <!-- {ids} -->")
    if len(columns) > MAX_TABLE_COLUMNS:
        lines.append(f"\n_Only the first {MAX_TABLE_COLUMNS} of {len(columns)} columns are shown; source.json has all cells._")
    return lines


def to_markdown(bundle, title=None):
    metadata = bundle.get("metadata", {})
    lines = [f"# {title or metadata.get('filename', 'Document')}", "",
             f"_Source reader: {metadata.get('reader', 'unknown')}. Derived view; evidence lives in source.json._", ""]
    groups = {}
    for block in bundle["blocks"]:
        location = block.get("location", {})
        key = (location.get("page") or 1, location.get("sheet"))
        groups.setdefault(key, []).append(block)
    for (page, sheet), blocks in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        lines.append(f"## Sheet: {sheet}" if sheet else f"## Page {page}")
        lines.append("")
        if sheet and all("row" in b["location"] and "column" in b["location"] for b in blocks):
            lines.extend(_sheet_table(blocks))
        else:
            for block in blocks:
                text = block["text"].rstrip()
                lines.append(f"{text} <!-- {block['id']} -->")
        lines.append("")
    return "\n".join(lines)
