"""Generate labeled synthetic invoices; no network or pipeline imports.

Families (including all format replicas) are assigned to one split before rendering.
Labels describe visible document values, not recomputed/corrected totals. This small
synthetic corpus is not a substitute for independently annotated real documents.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
from decimal import Decimal
from pathlib import Path

FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name", "currency", "subtotal", "tax", "total")
FORMATS = ("pdf", "jpg", "png", "tiff", "xlsx", "xls", "csv")
SEEDS = {"train": 10713, "dev": 27109, "test": 93047}


def invoice(split: str, family: int, variant: int = 0) -> dict:
    rng = random.Random(SEEDS[split] + family * 101 + variant)
    price = Decimal(rng.randrange(1200, 8500)) / 100
    other = Decimal(rng.randrange(200, 900)) / 100
    subtotal = price * 2 + other
    tax = (subtotal * Decimal("0.10")).quantize(Decimal("0.01"))
    total = subtotal + tax + (Decimal("7.00") if family == 2 else 0)
    money = lambda value: format(value, ".2f")
    return {
        "invoice_number": f"{split.upper()}-{SEEDS[split]}-{family}-{variant}",
        "invoice_date": f"2026-{rng.randrange(1, 13):02d}-{rng.randrange(1, 28):02d}",
        "supplier_name": f"{split.title()} Cedar {rng.randrange(1000,9999)} Ltd",
        "customer_name": None if family == 1 else f"{split.title()} Harbor {rng.randrange(1000,9999)} Co",
        "currency": ("USD", "EUR", "GBP")[family % 3],
        "subtotal": money(subtotal), "tax": money(tax), "total": money(total),
        "line_items": [
            {"description": f"Precision bolts {split}", "quantity": "2", "unit_price": money(price), "line_total": money(price * 2)},
            {"description": f"Packing service {split}", "quantity": "1", "unit_price": money(other), "line_total": money(other)},
        ],
        "needs_review": family in (1, 2),
    }


def rows(inv: dict, layout: int = 0) -> list[list[str]]:
    labels = ["Invoice Number", "Invoice Date", "Supplier Name", "Customer Name", "Currency", "Subtotal", "Tax", "Total"]
    result = [["INVOICE", ""]]
    pairs = list(zip(FIELDS, labels))
    if layout == 1:
        pairs = pairs[:5] + pairs[6:] + pairs[5:6]
    for field, label in pairs:
        if inv[field] is not None:
            result.append([label, inv[field]])
    result += [[""], ["Description", "Quantity", "Unit Price", "Line Total"]]
    result += [[item[key] for key in ("description", "quantity", "unit_price", "line_total")] for item in inv["line_items"]]
    return result


def text(inv: dict, layout: int = 0) -> str:
    return "\n".join((f"{row[0]}: {row[1]}" if len(row) == 2 else " | ".join(row)) for row in rows(inv, layout))


def image(inv: dict, layout: int = 0, poor: bool = False):
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    canvas = Image.new("RGB", (1600, 1600), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 29)
    except OSError:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 29)
        except OSError:
            font = ImageFont.load_default(size=29)
    draw.multiline_text((70, 70), text(inv, layout), fill="black", font=font, spacing=18)
    if poor:
        canvas = canvas.resize((800, 800)).resize((1600, 1600)).filter(ImageFilter.GaussianBlur(1.1)).rotate(1.2, fillcolor="white")
    return canvas


def render(path: Path, invoices: list[dict], layout: int = 0, scanned: bool = False, poor: bool = False) -> None:
    ext = path.suffix.lower()
    if ext == ".pdf":
        import fitz
        with fitz.open() as doc:
            for inv in invoices:
                page = doc.new_page(width=700, height=800)
                if scanned:
                    buffer = io.BytesIO()
                    image(inv, layout, poor).save(buffer, format="PNG")
                    page.insert_image(page.rect, stream=buffer.getvalue())
                else:
                    page.insert_text((45, 45), text(inv, layout), fontsize=12)
            doc.save(path, garbage=4, deflate=True, no_new_id=True)
    elif ext in (".jpg", ".png", ".tiff"):
        frames = [image(inv, layout, poor) for inv in invoices]
        if ext == ".tiff":
            frames[0].save(path, save_all=True, append_images=frames[1:], compression="tiff_deflate")
        else:
            frames[0].save(path)
    elif ext == ".xlsx":
        from openpyxl import Workbook
        from datetime import datetime
        book = Workbook()
        book.remove(book.active)
        book.properties.created = book.properties.modified = datetime(2026, 1, 1)
        for i, inv in enumerate(invoices):
            sheet = book.create_sheet(f"Invoice {i+1}")
            for row in rows(inv, layout):
                sheet.append(row)
        book.save(path)
    elif ext == ".xls":
        import xlwt
        book = xlwt.Workbook()
        for i, inv in enumerate(invoices):
            sheet = book.add_sheet(f"Invoice {i+1}")
            for y, row in enumerate(rows(inv, layout)):
                for x, value in enumerate(row):
                    sheet.write(y, x, value)
        book.save(str(path))
    else:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";" if layout else ",")
            for inv in invoices:
                writer.writerows(rows(inv, layout))


def generate(output: Path, long_pages: int = 0) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "seed_by_split": SEEDS, "synthetic": True,
                "real_sample_slice": {"status": "pending", "count": 0, "reason": "No approved real samples supplied; no downloads performed."},
                "ground_truth_status": "Generator-derived, arithmetic self-checked; not independently human annotated.",
                "split_policy": "Family assigned before rendering. All related formats/pages/sheets stay within one split. Test is held out; no test-based tuning.",
                "documents": []}

    def add(split, family, name, fmt, invoices, category="clean", **kwargs):
        folder = output / split
        folder.mkdir(exist_ok=True)
        path = folder / f"{name}.{fmt}"
        render(path, invoices, layout=SEEDS[split] % 2, **kwargs)
        manifest["documents"].append({"id": f"{split}/{name}.{fmt}", "path": path.relative_to(output).as_posix(),
            "split": split, "family": f"{split}-family-{family}", "format": fmt,
            "category": category, "expected_failure": False, "expected_units": len(invoices),
            "invoices": invoices, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    for split in SEEDS:
        for family, category in enumerate(("clean", "missing_field", "wrong_total")):
            inv = invoice(split, family)
            for fmt in FORMATS:
                add(split, family, f"family-{family}-{category}", fmt, [inv], category)
        multi = [invoice(split, 3, index) for index in range(2)]
        for fmt in ("pdf", "tiff", "xlsx", "xls"):
            add(split, 3, "family-3-multi", fmt, multi, "multi_invoice")
        add(split, 4, "family-4-scanned", "pdf", [invoice(split, 4)], "scanned", scanned=True)
        add(split, 5, "family-5-poor-scan", "png", [invoice(split, 5)], "poor_scan", poor=True)
        for fmt in FORMATS:
            path = output / split / f"corrupt.{fmt}"
            path.write_bytes(b"\x00\xffCORRUPT-TRUNCATED\x00\xfe")
            manifest["documents"].append({"id": f"{split}/corrupt.{fmt}", "path": path.relative_to(output).as_posix(),
                "split": split, "family": f"{split}-corrupt-{fmt}", "format": fmt, "category": "corrupt",
                "expected_failure": True, "expected_units": 0, "invoices": [],
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if long_pages:
        add("test", 99, f"family-99-long-{long_pages}", "pdf", [invoice("test", 99, i) for i in range(long_pages)], "long_pdf")
    for doc in manifest["documents"]:
        for inv in doc["invoices"]:
            assert sum(Decimal(item["line_total"]) for item in inv["line_items"]) == Decimal(inv["subtotal"])
            assert (Decimal(inv["subtotal"]) + Decimal(inv["tax"]) != Decimal(inv["total"])) == (doc["category"] == "wrong_total")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/data"))
    parser.add_argument("--long-pages", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.long_pages <= 1500:
        parser.error("--long-pages must be between 0 and 1500")
    manifest = generate(args.output, args.long_pages)
    print(f"Generated {len(manifest['documents'])} labeled synthetic documents at {args.output}")


if __name__ == "__main__":
    main()
