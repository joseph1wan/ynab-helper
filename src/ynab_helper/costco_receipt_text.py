"""Parse manually copy-pasted Costco receipt page text.

Costco has no live scraper (unlike Target) — this is the only ingestion path.
Copy a receipt's rendered text (select-all + copy from the order-history
detail page) into a .txt file; see `costco_import.py` for the file-level
orchestration (inbox draining, archiving) that uses this module.

Costco has two receipt layouts, detected from the title line:

**Gas Station Receipt** — alternating Label\\nValue lines, like Target's
invoice text:

    Gas Station Receipt
    Lake In The Hills #774
    250 N RANDALL RD
    LAKE IN THE HILLS, IL 60156
    Member
    111948273562
    Invoice#
    16397
    Date:
    07/16/26
    Time:
    15:48
    ...
    Product
    Amount
    Regular
    $54.69
    Total Sale
    $54.69
    ...

**In-Warehouse Receipt** — tabular columns for line items (tolerant of tabs
vs. runs of spaces from copy-paste), item prices listed pre-discount with a
discount line immediately following the item it applies to:

    In-Warehouse Receipt

    Costco Wholesale
    LAKE IN THE HILLS #774
    ...
    Member 111948273562
    221663	**KLNX ULTR*	21.39 Y
    381838	/ 221663	5.00-
    512599	**KS TOWEL**	20.79 Y
    ...
    SUBTOTAL	77.65
    TAX	4.30
    ****	TOTAL	81.95
    ...
    Whse: 774
    Trm: 11
    Trn: 439
    ...
    P7 07/16/2026 03:38

Each item line is `[E ]<item_code> <name> <price>[ <tax_code>]`; a discount
line `<code> / <orig_item_code> <amount>-` immediately follows the item it
applies to and must be netted into that item's line_total (item prices sum
to the pre-discount total; discount lines subtract down to SUBTOTAL). The
"INSTANT SAVINGS" summary line near the footer is already reflected by the
per-item discount lines and must never be subtracted a second time.

Costco has no order_id/invoice_id pair like Target. The stable identifier is
a composite of warehouse number, date, and transaction number:
`{store_number}_{receipt_date}_{transaction_number}`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ynab_helper.models import LineItem
from ynab_helper.target_scraper import _to_milliunits


@dataclass
class ParsedReceipt:
    receipt_id: str
    receipt_type: str  # "gas" | "warehouse"
    store_number: str
    transaction_number: str
    receipt_date: date
    total: int  # milliunits (positive)
    tax: int = 0
    line_items: list[LineItem] = field(default_factory=list)


def _parse_costco_date(value: str) -> date:
    """Parse 'MM/DD/YY' (gas receipts) or 'MM/DD/YYYY' (warehouse receipts)."""
    match = re.match(r"^(\d{2})/(\d{2})/(\d{2}|\d{4})$", value.strip())
    if not match:
        raise ValueError(f"Unrecognized Costco receipt date: {value!r}")
    month, day, year = match.groups()
    if len(year) == 2:
        year = f"20{year}"
    return date(int(year), int(month), int(day))


def _amount_to_milliunits(value: str) -> int | None:
    cleaned = value.strip().lstrip("$")
    try:
        return _to_milliunits(cleaned)
    except (ValueError, TypeError):
        return None


def _find_value_after(lines: list[str], label: str) -> str | None:
    """Return the first non-blank line strictly after a line equal to `label`."""
    for i, line in enumerate(lines):
        if line.strip() == label:
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    return lines[j].strip()
            return None
    return None


def _store_number_from_header(lines: list[str]) -> str | None:
    for line in lines[:6]:
        match = re.search(r"#(\d+)", line)
        if match:
            return match.group(1)
    return None


def _make_receipt_id(store_number: str, receipt_date: date, transaction_number: str) -> str:
    return f"{store_number}_{receipt_date.isoformat()}_{transaction_number}"


def detect_receipt_type(text: str) -> str | None:
    """Scan for the title row. Returns 'gas' | 'warehouse' | None.

    A select-all copy of the Orders & Purchases page includes the order
    history listing (and, for gas receipts, a "Gas Station" row per order)
    *before* the actual receipt content, so the title line isn't
    necessarily near the top — scan the whole text rather than just the
    first few lines."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "Gas Station Receipt":
            return "gas"
        if stripped == "In-Warehouse Receipt":
            return "warehouse"
    return None


def _slice_from_title(text: str, title: str) -> list[str] | None:
    """Return lines starting at the exact `title` line, or None if absent.

    A select-all copy of the Orders & Purchases page prepends the order
    history listing (and, for gas orders, one "Gas Station" row per order)
    before the actual receipt — slicing to the title keeps header/footer
    lookups below from matching listing content instead of the receipt."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == title:
            return lines[i:]
    return None


def parse_gas_receipt_text(text: str) -> ParsedReceipt | None:
    lines = _slice_from_title(text, "Gas Station Receipt")
    if lines is None:
        return None

    store_number = _store_number_from_header(lines)
    if not store_number:
        return None

    transaction_number = _find_value_after(lines, "Invoice#")
    if not transaction_number:
        return None

    date_str = _find_value_after(lines, "Date:")
    if not date_str:
        return None
    try:
        receipt_date = _parse_costco_date(date_str)
    except ValueError:
        return None

    # "Product\nAmount\n<name>\n<price>" is a two-column table header
    # ("Product" | "Amount") followed by one data row — the value right
    # after "Product" is the "Amount" header label, not the product name.
    product_idx = next((i for i, line in enumerate(lines) if line.strip() == "Product"), None)
    if product_idx is None:
        return None
    header_and_row = [line.strip() for line in lines[product_idx + 1 :] if line.strip()][:3]
    if len(header_and_row) < 2 or header_and_row[0] != "Amount":
        return None
    product_name = header_and_row[1]

    total_str = _find_value_after(lines, "Total Sale")
    total = _amount_to_milliunits(total_str) if total_str else None
    if total is None:
        return None

    line_item = LineItem(name=f"Costco Gas - {product_name}", quantity=1, line_total=total)

    return ParsedReceipt(
        receipt_id=_make_receipt_id(store_number, receipt_date, transaction_number),
        receipt_type="gas",
        store_number=store_number,
        transaction_number=transaction_number,
        receipt_date=receipt_date,
        total=total,
        tax=0,
        line_items=[line_item],
    )


_ITEM_RE = re.compile(r"^(?:E )?(\d+) (.+?) (\d+\.\d{2})\s*([A-Za-z0-9])?$")
_DISCOUNT_RE = re.compile(r"^(\d+) / (\d+) (\d+\.\d{2})-$")
# A second discount format seen on some receipts: "<code> #<tag> <amount>-"
# (e.g. "382390 #GATORADE 9.90-") — no "/ <orig item code>" back-reference,
# so (like the item-code discount above) it's netted into whichever item was
# added immediately before it.
_DISCOUNT_TAG_RE = re.compile(r"^\d+ #\S+ (\d+\.\d{2})-$")
# Weighed/multi-unit annotation line preceding the item it describes, e.g.
# "2 @ 4.49" (2 units at $4.49 each) — informational only, not its own item.
_QTY_ANNOTATION_RE = re.compile(r"^\d+\s*@\s*\d+\.\d{2}$")


def _line_value_after_label(lines: list[str], label: str) -> str | None:
    """Costco's SUBTOTAL/TAX/TOTAL rows are 'LABEL<ws>AMOUNT' on one line
    (optionally prefixed by '****' for TOTAL), unlike Target's separate
    label/value lines."""
    pattern = re.compile(rf"^\**\s*{re.escape(label)}\s+\$?([\d,]+\.\d{{2}})", re.IGNORECASE)
    for line in lines:
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    return None


def parse_warehouse_receipt_text(text: str) -> ParsedReceipt | None:
    lines = _slice_from_title(text, "In-Warehouse Receipt")
    if lines is None:
        return None

    whse_match = next(
        (re.search(r"Whse:\s*(\d+)", line) for line in lines if "Whse:" in line), None
    )
    store_number = whse_match.group(1) if whse_match else _store_number_from_header(lines)
    if not store_number:
        return None

    trn_match = next((re.search(r"Trn:\s*(\d+)", line) for line in lines if "Trn:" in line), None)
    transaction_number = trn_match.group(1) if trn_match else None
    if not transaction_number:
        return None

    receipt_date = None
    for line in lines:
        match = re.match(r"^P\d+\s+(\d{2}/\d{2}/\d{4})\s+\d{2}:\d{2}", line.strip())
        if match:
            try:
                receipt_date = _parse_costco_date(match.group(1))
            except ValueError:
                receipt_date = None
            break
    if receipt_date is None:
        for line in lines:
            match = re.search(r"(\d{2}/\d{2}/\d{4})\d{2}:\d{2}", line)
            if match:
                try:
                    receipt_date = _parse_costco_date(match.group(1))
                except ValueError:
                    receipt_date = None
                break
    if receipt_date is None:
        return None

    # Bound the item/discount scan to between "Member <digits>"/"barcode" and
    # "SUBTOTAL", so payment/card-auth footer lines below are never mistaken
    # for item or discount lines.
    start_idx = next(
        (i for i, line in enumerate(lines) if re.match(r"^(Member\s+\d+|barcode)$", line.strip())),
        None,
    )
    end_idx = next((i for i, line in enumerate(lines) if line.strip().upper().startswith("SUBTOTAL")), None)
    if start_idx is None or end_idx is None or end_idx <= start_idx:
        return None

    items_by_code: dict[str, LineItem] = {}
    order: list[str] = []
    for raw_line in lines[start_idx + 1 : end_idx]:
        normalized = re.sub(r"\s+", " ", raw_line.strip())
        if not normalized:
            continue

        if _QTY_ANNOTATION_RE.match(normalized):
            continue

        discount_match = _DISCOUNT_RE.match(normalized)
        if discount_match:
            _discount_code, orig_code, amount_str = discount_match.groups()
            amount = _to_milliunits(amount_str)
            if orig_code in items_by_code:
                items_by_code[orig_code].line_total -= amount
            continue

        tag_discount_match = _DISCOUNT_TAG_RE.match(normalized)
        if tag_discount_match:
            amount = _to_milliunits(tag_discount_match.group(1))
            if order:
                items_by_code[order[-1]].line_total -= amount
            continue

        item_match = _ITEM_RE.match(normalized)
        if item_match:
            code, name, price_str, _tax_code = item_match.groups()
            items_by_code[code] = LineItem(
                name=name.strip("* "), quantity=1, line_total=_to_milliunits(price_str)
            )
            order.append(code)
            continue

    line_items = [items_by_code[code] for code in order]
    if not line_items:
        return None

    subtotal_str = _line_value_after_label(lines, "SUBTOTAL")
    tax_str = _line_value_after_label(lines, "TAX")
    total_str = _line_value_after_label(lines, "TOTAL")
    if subtotal_str is None or tax_str is None or total_str is None:
        return None

    subtotal = _to_milliunits(subtotal_str)
    tax = _to_milliunits(tax_str)
    total = _to_milliunits(total_str)

    # Cross-validate rather than silently emit a receipt whose numbers don't
    # reconcile (mirrors Target's parser's fail-clean-on-bad-anchor stance).
    if abs((subtotal + tax) - total) > 10:
        return None
    if abs(sum(item.line_total for item in line_items) - subtotal) > 10:
        return None

    return ParsedReceipt(
        receipt_id=_make_receipt_id(store_number, receipt_date, transaction_number),
        receipt_type="warehouse",
        store_number=store_number,
        transaction_number=transaction_number,
        receipt_date=receipt_date,
        total=total,
        tax=tax,
        line_items=line_items,
    )


def parse_receipt_text(text: str) -> ParsedReceipt | None:
    """Public entry point: dispatches to the gas or warehouse parser by
    detected receipt type. Returns None (rather than raising) on any failure
    so callers can report a clean per-file error instead of crashing an
    entire batch import."""
    if not text or not text.strip():
        return None

    receipt_type = detect_receipt_type(text)
    if receipt_type == "gas":
        return parse_gas_receipt_text(text)
    if receipt_type == "warehouse":
        return parse_warehouse_receipt_text(text)
    return None
