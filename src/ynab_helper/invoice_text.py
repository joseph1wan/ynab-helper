"""Parse manually copy-pasted Target invoice page text.

Fallback path for when the Playwright scraper is soft-blocked (or captures
mid-hydration HTML that yields $0 line items): copy the rendered invoice page
text (select-all + copy, no need for page source), save it to a .txt file,
and parse it here instead. See `invoice_import.py` for the file-level
orchestration (inbox draining, archiving) that uses this module.

The pasted text looks like (one field per line, in this order):

    Orders/
    912003599332151/
    Invoices/
    61973991383936767
    Invoice 2 of 2
    ...
    Invoice date: Thu, Jul 16, 2026
    Invoice number: 61973991383936767
    Item
    94852290 - Baby 3pk Cotton Jogger Pull-On Pants - Cloud Island(TM) ...
    Qty.
    1
    Unit price
    $12.00
    Amount
    $12.00
    Item subtotal
    $12.00
    Sales tax
    $0.99
    Item total
    $12.99
    ... (repeats per item)
    Invoice total
    $72.81
    Digital MFR Coupon        <- payment/adjustment lines below Invoice total,
    $8.00                        never treated as line items
    Target GiftCard
    $50.00
    ...

We deliberately stop scanning for items once "Invoice total" is seen, since
giftcard/coupon/adjustment rows below it look structurally similar (a label
line followed by a $ amount line) and must not be parsed as products.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ynab_helper.models import LineItem
from ynab_helper.target_scraper import _parse_date, _to_milliunits


@dataclass
class ParsedInvoice:
    order_id: str
    invoice_id: str
    order_date_str: str
    total: int
    card_total: int
    line_items: list[LineItem] = field(default_factory=list)


# Payment-summary lines below "Invoice total" that don't represent a charge
# to a trackable bank/credit account — gift cards and coupon/adjustment rows
# never post as a YNAB transaction, so their amounts must be excluded when
# computing the actual card-charged total used for matching.
_NON_CARD_PAYMENT_LABELS = (
    "Target GiftCard",
    "Digital MFR Coupon",
    "General Ledger Adjustment",
)

_PAYMENT_SUMMARY_END_MARKERS = (
    "Help us improve this experience.",
    "Get top deals, latest trends, and more.",
)


def _parse_card_total(lines: list[str], start_idx: int, invoice_total: int) -> int:
    """Sum the payment-summary rows after "Invoice total"/"Total refund" and
    return the portion actually charged to a card (i.e. what will show up as
    a YNAB transaction), excluding gift cards and coupon/adjustment rows.

    A payment line with no following $ amount means it's the sole payment
    method for the invoice, so its amount is implied to be whatever's left
    of invoice_total after the other stated amounts.
    """
    entries: list[tuple[str, int | None]] = []
    i = start_idx
    while i < len(lines):
        label = lines[i].strip()
        if not label or label in _PAYMENT_SUMMARY_END_MARKERS:
            break
        amount = None
        if i + 1 < len(lines) and lines[i + 1].strip().startswith("$"):
            amount = _amount_to_milliunits(lines[i + 1].strip())
            i += 2
        else:
            i += 1
        entries.append((label, amount))

    explicit_sum = sum(amount for _, amount in entries if amount is not None)
    non_card_total = 0
    for label, amount in entries:
        if amount is None:
            amount = invoice_total - explicit_sum
        if label in _NON_CARD_PAYMENT_LABELS:
            non_card_total += amount

    return invoice_total - non_card_total


def _find_value_after(lines: list[str], label: str) -> str | None:
    """Return the first non-blank line strictly after a line equal to `label`."""
    for i, line in enumerate(lines):
        if line.strip() == label:
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    return lines[j].strip()
            return None
    return None


def _amount_to_milliunits(value: str) -> int | None:
    cleaned = value.strip().lstrip("$")
    negative = cleaned.startswith("-")
    cleaned = cleaned.lstrip("-")
    try:
        milliunits = _to_milliunits(cleaned)
    except (ValueError, TypeError):
        return None
    return -milliunits if negative else milliunits


def parse_invoice_text(text: str) -> ParsedInvoice | None:
    """Parse pasted Target invoice page text into a ParsedInvoice.

    Returns None (rather than raising) when required anchors are missing,
    so callers can report a clean per-file error instead of crashing an
    entire batch import.
    """
    if not text or not text.strip():
        return None

    lines = text.splitlines()

    invoice_id = _find_value_after(lines, "Invoices/")
    invoice_number_line = next(
        (line for line in lines if line.strip().lower().startswith("invoice number:")), None
    )
    if invoice_number_line:
        invoice_id = invoice_number_line.split(":", 1)[1].strip()
    if not invoice_id or not invoice_id.isdigit():
        return None

    order_id = _find_value_after(lines, "Orders/")
    if order_id:
        order_id = order_id.rstrip("/")
    if not order_id or not order_id.isdigit():
        return None

    date_line = next(
        (line for line in lines if line.strip().lower().startswith("invoice date:")), None
    )
    if not date_line:
        return None
    order_date_str = date_line.split(":", 1)[1].strip()

    # Find the "Invoice total" boundary (or "Total refund" on refund invoices);
    # only scan for items before it so payment/adjustment rows after it are
    # never mistaken for line items.
    total_idx = next(
        (i for i, line in enumerate(lines) if line.strip() in ("Invoice total", "Total refund")),
        None,
    )
    if total_idx is None:
        return None
    total_value = None
    total_value_idx = None
    for j in range(total_idx + 1, len(lines)):
        if lines[j].strip():
            total_value = lines[j].strip()
            total_value_idx = j
            break
    total = _amount_to_milliunits(total_value) if total_value else None
    if total is None:
        return None
    card_total = _parse_card_total(lines, total_value_idx + 1, total)

    item_lines = lines[:total_idx]
    items: list[LineItem] = []
    for i, line in enumerate(item_lines):
        if line.strip() != "Item":
            continue

        # Bound this item's block to just before the next "Item" line so a
        # missing field can't accidentally pull a value from the next item.
        next_item_idx = next(
            (j for j in range(i + 1, len(item_lines)) if item_lines[j].strip() == "Item"),
            len(item_lines),
        )
        block = item_lines[i:next_item_idx]

        name_raw = next((line.strip() for line in block[1:] if line.strip()), None)
        if not name_raw:
            continue
        name = name_raw
        # Strip leading TCIN prefix like "94852290 - "
        if " - " in name:
            prefix, rest = name.split(" - ", 1)
            if prefix.strip().isdigit():
                name = rest.strip()

        qty_value = _find_value_after(block, "Qty.")
        try:
            quantity = int(qty_value) if qty_value else 1
        except ValueError:
            quantity = 1

        amount_value = _find_value_after(block, "Amount")
        line_total = _amount_to_milliunits(amount_value) if amount_value else None
        if line_total is None:
            continue

        items.append(LineItem(name=name, quantity=quantity, line_total=line_total))

    if not items:
        return None

    return ParsedInvoice(
        order_id=order_id,
        invoice_id=invoice_id,
        order_date_str=order_date_str,
        total=total,
        card_total=card_total,
        line_items=items,
    )


def parsed_invoice_order_date(parsed: ParsedInvoice):
    """Parse the free-text invoice date (e.g. 'Thu, Jul 16, 2026') into a date."""
    # _parse_date's known formats don't include the "Thu, Jul 16, 2026" shape
    # invoices use, so strip a leading weekday if present before delegating.
    value = parsed.order_date_str
    if "," in value:
        parts = [p.strip() for p in value.split(",")]
        if len(parts) == 3:
            # ["Thu", "Jul 16", "2026"] -> "Jul 16, 2026"
            value = f"{parts[1]}, {parts[2]}"
    return _parse_date(value)
