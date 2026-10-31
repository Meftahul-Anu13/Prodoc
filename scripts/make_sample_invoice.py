"""Generate a fictional 4-page invoice pack with an answer key (no real data).

Pages 1-3: one invoice whose line table continues across pages, totals on page 3.
Page 4: a second, separate invoice from the same supplier.
Usage: python -m scripts.make_sample_invoice [output_dir]
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

import fitz

SUPPLIER = ("Northwind Office Supply Ltd.", "42 Harbour Road, Portsmouth PO1 3AX, UK", "VAT No. GB 123 4567 89")
CUSTOMER = ("Contoso Engineering GmbH", "Industriestrasse 7, 80331 Munich, Germany")
ITEMS = [
    ("A4 copy paper, 80gsm (box of 5 reams)", 40, "18.50"), ("Black toner cartridge TN-2420", 12, "64.90"),
    ("Ergonomic office chair, mesh back", 6, "189.00"), ("Height-adjustable desk 160x80", 4, "412.00"),
    ("Whiteboard markers, assorted (pack of 12)", 25, "9.75"), ("Lever arch files A4 (pack of 10)", 30, "14.20"),
    ("Wireless keyboard and mouse set", 15, "38.40"), ("27-inch IPS monitor", 8, "229.00"),
    ("USB-C docking station", 8, "119.50"), ("Desk organiser, bamboo", 20, "16.80"),
    ("Shredder, cross-cut, 20 sheets", 2, "245.00"), ("Laminating pouches A4 (pack of 100)", 10, "12.60"),
    ("Coffee capsules (box of 100)", 18, "32.00"), ("First aid kit, workplace 25 persons", 3, "54.30"),
    ("Cable management tray", 12, "21.90"), ("Noise-cancelling headset", 10, "89.00"),
    ("Desk lamp LED, dimmable", 10, "34.50"), ("Archive storage boxes (pack of 10)", 15, "27.40"),
    ("Sticky notes 76x76 (pack of 24)", 20, "11.90"), ("Installation and assembly service (hours)", 14, "45.00"),
]
SECOND = [("Printer servicing, annual contract", 1, "480.00"), ("Replacement drum unit DR-2400", 4, "72.50")]
VAT = Decimal("0.20")
MONEY = Decimal("0.01")


def money(value):
    return f"{value.quantize(MONEY):,.2f}"


def write(page, y, text, x=50, size=10, bold=False):
    page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")


def header(page, number, date, due, title="INVOICE"):
    write(page, 60, title, size=20, bold=True)
    write(page, 60, f"Invoice No: {number}", x=360, bold=True)
    write(page, 76, f"Invoice Date: {date}", x=360)
    write(page, 92, f"Due Date: {due}", x=360)
    write(page, 92, SUPPLIER[0], bold=True)
    write(page, 106, SUPPLIER[1])
    write(page, 120, SUPPLIER[2])
    write(page, 150, "Bill To:", bold=True)
    write(page, 164, CUSTOMER[0])
    write(page, 178, CUSTOMER[1])


def table_head(page, y):
    for x, label in ((50, "Description"), (330, "Qty"), (390, "Unit Price"), (480, "Amount")):
        write(page, y, label, x=x, bold=True)
    page.draw_line((50, y + 5), (545, y + 5))
    return y + 22


def rows(page, y, items):
    lines, total = [], Decimal(0)
    for description, qty, price in items:
        amount = Decimal(price) * qty
        total += amount
        write(page, y, description)
        write(page, y, str(qty), x=330)
        write(page, y, money(Decimal(price)), x=390)
        write(page, y, money(amount), x=480)
        lines.append({"description": description, "quantity": str(qty), "unit_price": money(Decimal(price)).replace(",", ""),
                      "line_total": money(amount).replace(",", "")})
        y += 20
    return y, lines, total


def footer(page, number, of):
    write(page, 810, f"Page {number} of {of}", x=270, size=8)


def totals(page, y, subtotal, currency="GBP"):
    tax = (subtotal * VAT).quantize(MONEY)
    total = subtotal + tax
    page.draw_line((330, y), (545, y))
    for label, value in (("Subtotal", subtotal), ("VAT 20%", tax), (f"Total Due ({currency})", total)):
        y += 20
        write(page, y, label, x=330, bold=label.startswith("Total"))
        write(page, y, money(value), x=480, bold=label.startswith("Total"))
    return tax, total


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "var/samples")
    output.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    key = []

    # Invoice 1 spans pages 1-3.
    page = document.new_page(width=595, height=842)
    header(page, "NW-2026-0417", "14 October 2026", "13 November 2026")
    write(page, 205, "All amounts in GBP. Prices exclude VAT.", size=9)
    y, first, subtotal = rows(page, table_head(page, 230), ITEMS[:9])
    write(page, y + 10, "Continued on next page ...", size=9)
    footer(page, 1, 4)
    page = document.new_page(width=595, height=842)
    write(page, 60, "Invoice No: NW-2026-0417 (continued)", bold=True)
    y, more, part = rows(page, table_head(page, 95), ITEMS[9:18])
    first += more
    subtotal += part
    write(page, y + 10, "Continued on next page ...", size=9)
    footer(page, 2, 4)
    page = document.new_page(width=595, height=842)
    write(page, 60, "Invoice No: NW-2026-0417 (continued)", bold=True)
    y, more, part = rows(page, table_head(page, 95), ITEMS[18:])
    first += more
    subtotal += part
    tax, total = totals(page, y + 10, subtotal)
    write(page, y + 110, "Payment terms: 30 days. Bank: Fictional Bank plc, IBAN GB00 FAKE 0000 0000 0000 00", size=9)
    footer(page, 3, 4)
    key.append({"invoice_number": "NW-2026-0417", "invoice_date": "14 October 2026", "supplier_name": SUPPLIER[0],
                "customer_name": CUSTOMER[0], "currency": "GBP", "subtotal": str(subtotal.quantize(MONEY)),
                "tax": str(tax), "total": str(total), "pages": "1-3", "line_items": first})

    # Invoice 2 on page 4.
    page = document.new_page(width=595, height=842)
    header(page, "NW-2026-0418", "21 October 2026", "20 November 2026")
    write(page, 205, "All amounts in GBP. Prices exclude VAT.", size=9)
    y, second, subtotal = rows(page, table_head(page, 230), SECOND)
    tax, total = totals(page, y + 10, subtotal)
    footer(page, 4, 4)
    key.append({"invoice_number": "NW-2026-0418", "invoice_date": "21 October 2026", "supplier_name": SUPPLIER[0],
                "customer_name": CUSTOMER[0], "currency": "GBP", "subtotal": str(subtotal.quantize(MONEY)),
                "tax": str(tax), "total": str(total), "pages": "4", "line_items": second})

    pdf = output / "northwind-invoice-pack-4-pages.pdf"
    document.save(pdf)
    (output / "northwind-invoice-pack-4-pages.answer-key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(f"Wrote {pdf}\n      {pdf.with_suffix('').as_posix()}.answer-key.json")
    for invoice in key:
        print(f"  {invoice['invoice_number']} (pages {invoice['pages']}): {len(invoice['line_items'])} lines, "
              f"subtotal {invoice['subtotal']}, VAT {invoice['tax']}, total {invoice['total']} GBP")


if __name__ == "__main__":
    main()
