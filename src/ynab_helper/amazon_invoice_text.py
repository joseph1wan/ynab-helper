"""Parse manually copy-pasted Amazon order-confirmation/invoice page text.

Amazon has no live scraper (unlike Target) — this is the only ingestion path.
Copy an order's rendered text (select-all + copy from the order-detail page)
into a .txt file; see `amazon_import.py` for the file-level orchestration
(inbox draining, archiving) that uses this module.

Unlike Costco's fixed-width monospace POS receipts, Amazon's paste shape is
markdown-link-style items interspersed with plain prose, e.g.:

    Order Summary
    Order placed
    July 24, 2026
    Order #
    111-1239029-5887460
    ...
    Order Summary
    * Item(s) Subtotal:
    $28.72
    * Shipping & Handling:
    $2.99
    * Free Shipping:
    -$2.99
    * Estimated tax to be collected:
    $2.01
    * Grand Total:
    $30.73

    Delivered July 25
    [Product name](url)
    Sold by: [Seller](url)
    Return or replace items: Eligible through August 24, 2026
    $6.78
    [Product name 2](url)
    ...
    $11.97

Each total is a `* Label:` line followed by a `$Amount` line (Costco's gas
receipts have this same label-then-value shape, just different label text).
Only the labels this parser actually needs are looked up — an order may also
contain a `Gift Card Amount` row, a `Total before tax` row, or some other
label never seen before; those are simply not looked up and have no effect on
parsing. `Grand Total` is read directly and is the only total value matched
against a YNAB transaction — nothing is computed from the other rows.

Each item is a multi-line block: an optional bare quantity line (e.g. `2`,
seen directly above the item name when quantity > 1 — default 1 when absent),
the item name (either a markdown link `[Name](url)` or a bare line, depending
on how the page was copied), then `Sold by:` / `Supplied by:` / `Return ...`
/ `Delivered ...` lines to skip, and finally a bare `$X.XX` price line that
closes out the item (this is the line total for that quantity, not a
per-unit price — Amazon doesn't show a separate unit-price breakdown).

Order id is a single stable field (`Order #`), unlike Costco's composite
store/date/transaction id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from ynab_helper.models import LineItem
from ynab_helper.target_scraper import _to_milliunits


@dataclass
class ParsedAmazonOrder:
    order_id: str
    order_date: date
    total: int  # milliunits (positive) — Grand Total
    tax: int = 0
    shipping: int = 0
    line_items: list[LineItem] = field(default_factory=list)


def _parse_amazon_date(value: str) -> date:
    """Parse Amazon's prose date format, e.g. 'July 24, 2026'."""
    try:
        return datetime.strptime(value.strip(), "%B %d, %Y").date()
    except ValueError as exc:
        raise ValueError(f"Unrecognized Amazon order date: {value!r}") from exc


def _amount_to_milliunits(value: str) -> int | None:
    cleaned = value.strip()
    negative = cleaned.startswith("-")
    if negative:
        cleaned = cleaned[1:]
    cleaned = cleaned.lstrip("$")
    try:
        amount = _to_milliunits(cleaned)
    except (ValueError, TypeError):
        return None
    return -amount if negative else amount


def _find_value_after(lines: list[str], label: str) -> str | None:
    """Return the first non-blank line strictly after a line equal to `label`."""
    for i, line in enumerate(lines):
        if line.strip() == label:
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    return lines[j].strip()
            return None
    return None


_ITEM_LINK_RE = re.compile(r"^\[(.+?)\]\(.*\)$")
_BARE_QTY_RE = re.compile(r"^\d+$")
_BARE_PRICE_RE = re.compile(r"^-?\$[\d,]+\.\d{2}$")
_SKIP_PREFIXES = ("Sold by:", "Supplied by:", "Return", "Delivered ")
# Fixed delivery-status prose lines that follow a "Delivered ..." header —
# not item names, but don't match a simple prefix either.
_SKIP_LINES = {
    "Your package was left near the front door or porch.",
}


def _extract_items(lines: list[str]) -> list[LineItem]:
    items: list[LineItem] = []
    pending_qty = 1
    pending_name: str | None = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if _BARE_QTY_RE.match(line):
            pending_qty = int(line)
            continue

        if _BARE_PRICE_RE.match(line):
            if pending_name is not None:
                unit_price = _amount_to_milliunits(line)
                if unit_price is not None:
                    items.append(
                        LineItem(
                            name=pending_name,
                            quantity=pending_qty,
                            line_total=unit_price * pending_qty,
                        )
                    )
            pending_qty = 1
            pending_name = None
            continue

        if line in _SKIP_LINES or any(line.startswith(prefix) for prefix in _SKIP_PREFIXES):
            continue

        if pending_name is not None:
            # Already have a pending name waiting on its price line — don't
            # let stray prose overwrite it.
            continue

        link_match = _ITEM_LINK_RE.match(line)
        pending_name = link_match.group(1) if link_match else line

    return items


def parse_invoice_text(text: str) -> ParsedAmazonOrder | None:
    """Public entry point. Returns None (rather than raising) on any anchor-
    not-found or reconciliation failure, so callers can report a clean
    per-file error instead of crashing an entire batch import."""
    if not text or not text.strip():
        return None

    lines = text.splitlines()

    order_id = _find_value_after(lines, "Order #")
    if not order_id:
        return None

    date_str = _find_value_after(lines, "Order placed")
    if not date_str:
        return None
    try:
        order_date = _parse_amazon_date(date_str)
    except ValueError:
        return None

    subtotal_str = _find_value_after(lines, "* Item(s) Subtotal:")
    subtotal = _amount_to_milliunits(subtotal_str) if subtotal_str else None
    if subtotal is None:
        return None

    tax_str = _find_value_after(lines, "* Estimated tax to be collected:")
    tax = _amount_to_milliunits(tax_str) if tax_str else None
    if tax is None:
        return None

    total_str = _find_value_after(lines, "* Grand Total:")
    total = _amount_to_milliunits(total_str) if total_str else None
    if total is None:
        return None

    shipping_str = _find_value_after(lines, "* Shipping & Handling:")
    shipping_and_handling = _amount_to_milliunits(shipping_str) if shipping_str else 0
    free_shipping_str = _find_value_after(lines, "* Free Shipping:")
    free_shipping = _amount_to_milliunits(free_shipping_str) if free_shipping_str else 0
    shipping = (shipping_and_handling or 0) + (free_shipping or 0)

    # Item rows appear after the totals block; anchoring the scan to start
    # after "Grand Total"'s own value line keeps the totals' $-lines from
    # ever being misread as item prices.
    grand_total_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == "* Grand Total:"), None
    )
    item_lines = lines[grand_total_idx + 1 :] if grand_total_idx is not None else lines
    items = _extract_items(item_lines)
    if not items:
        return None

    if abs(sum(item.line_total for item in items) - subtotal) > 10:
        return None

    return ParsedAmazonOrder(
        order_id=order_id,
        order_date=order_date,
        total=total,
        tax=tax,
        shipping=shipping,
        line_items=items,
    )
