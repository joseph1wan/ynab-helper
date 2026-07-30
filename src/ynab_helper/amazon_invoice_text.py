"""Parse manually copy-pasted Amazon order-confirmation/invoice page text.

Amazon has no live scraper (unlike Target) — this is the only ingestion path.
Copy an order's rendered text (select-all + copy from the order-detail page)
into a .txt file; see `amazon_import.py` for the file-level orchestration
(inbox draining, archiving) that uses this module.

Unlike Costco's fixed-width monospace POS receipts, Amazon's paste shape is
prose interspersed with item blocks, and real clipboard pastes are messier
than a clean rendering — e.g.:

    Order Summary
    Order placed June 21, 2026  Order # 114-2174468-2163434
    ...
    Order Summary
    Item(s) Subtotal:
    $15.38
    Shipping & Handling:
    $0.00
    Estimated tax to be collected:
    $1.54
    Gift Card Amount:
    -$16.46
    Grand Total:
    $0.46

    Delivered June 22
    Your package was left near the front door or porch.
    iDesign Slim Extra Long Clear Storage Bin2
    iDesign Slim Extra Long Clear Storage Bin, Narrow Stackable Organizer for Kitchen or Pantry
    Sold by: Amazon.com
    Supplied by: Other
    Return window closed on July 22, 2026
    $7.69

Notable real-world quirks this parser tolerates:

- "Order placed" and "Order #" can land on the SAME line (the page's
  Print/Ship-to layout collapses onto one row), so both are extracted with
  regex search over the whole text rather than by line position — `\\s`
  matches across newlines too, so the same regex works whether the label
  and value are on the same line or different ones.
- Total labels may or may not carry a "* " bullet prefix depending on how
  the page was copied; the bullet is optional in the match.
- Only the labels this parser actually needs are looked up — an order may
  contain a `Gift Card Amount` row, a `Total before tax` row, or some other
  label never seen before; those are simply not looked up and have no
  effect on parsing. `Grand Total` is read directly and is the only total
  value matched against a YNAB transaction — nothing is computed from the
  other rows.
- A return/refund order can carry an extra `Refund Total` row after Grand
  Total, followed by return-status prose ("Return complete", "Your return
  is complete...", "When will I get my refund?") before the actual item.
  `Refund Total` (or any other stray "Label \\n $Amount" pair appearing
  before the real item section) is distinguished from a real item because
  its price follows *immediately* — a real item name is always followed by
  at least one `Sold by:`/`Supplied by:`/`Return ...` line before its
  price. Such rows are skipped outright rather than looked up.
- Delivery/return status messages come in many different wordings ("left
  near the front door", "left in the mail room", "handed directly to a
  receptionist...") — rather than hardcode every phrasing, the item name
  is simply whichever non-skip, non-price, non-qty line was seen *most
  recently* before a price line closes out an item. Any stray prose
  between the previous item's price (or the totals block) and the real
  item name gets overwritten once the real name line is reached, so no
  specific wording needs to be recognized.
- An item's name can appear TWICE: once as a truncated image-alt-text
  preview with the quantity badge glued directly onto the end with no
  separator (`...Storage Bin2`), then again as the full title on the next
  line. When a candidate name line is immediately followed by another
  candidate name line, and stripping trailing digits from the first makes
  it a prefix of the second, the first is treated as the truncated preview
  (its trailing digits become the quantity) and the second becomes the
  real item name.
- Quantity may otherwise appear as its own bare line directly above the
  item name (no truncation involved). Either way, when quantity > 1 the
  price shown is a *per-unit* price, not the line total — Amazon doesn't
  show a separate line-total column — so `line_total = unit_price * qty`.

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


_ORDER_ID_RE = re.compile(r"Order #\s*(\S+)")
_ORDER_DATE_RE = re.compile(r"Order placed\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})")


def _find_order_id(text: str) -> str | None:
    match = _ORDER_ID_RE.search(text)
    return match.group(1) if match else None


def _find_order_date_str(text: str) -> str | None:
    match = _ORDER_DATE_RE.search(text)
    return match.group(1) if match else None


def _find_total(text: str, label: str) -> str | None:
    """Find a `<label>[:] <amount>` value anywhere in the text.

    `label` is a regex fragment (callers escape literal parens themselves,
    e.g. "Item\\(s\\) Subtotal"). Tolerant of an optional leading "* "
    bullet and of the label and amount landing on the same line or
    different lines (`\\s` matches newlines).
    """
    pattern = re.compile(
        rf"\*?\s*{label}:?\s*(-?\$[\d,]+\.\d{{2}})", re.IGNORECASE
    )
    match = pattern.search(text)
    return match.group(1) if match else None


_ITEM_LINK_RE = re.compile(r"^\[(.+?)\]\(.*\)$")
_BARE_QTY_RE = re.compile(r"^\d+$")
_BARE_PRICE_RE = re.compile(r"^-?\$[\d,]+\.\d{2}$")
_SKIP_PREFIXES = ("Sold by:", "Supplied by:", "Return", "Delivered ")
_TRAILING_DIGITS_RE = re.compile(r"^(.*\S)(\d+)$")
_GRAND_TOTAL_LINE_RE = re.compile(r"^\*?\s*Grand Total\b", re.IGNORECASE)


def _is_skip_line(line: str) -> bool:
    return any(line.startswith(prefix) for prefix in _SKIP_PREFIXES)


def _item_name_from_line(line: str) -> str:
    link_match = _ITEM_LINK_RE.match(line)
    return link_match.group(1) if link_match else line


def _is_candidate_name_line(line: str) -> bool:
    return not (_BARE_PRICE_RE.match(line) or _BARE_QTY_RE.match(line) or _is_skip_line(line))


def _next_non_blank(lines: list[str], idx: int) -> int:
    n = len(lines)
    while idx < n and not lines[idx].strip():
        idx += 1
    return idx


def _find_item_scan_start(lines: list[str]) -> int:
    """Find where real item rows begin, skipping past the totals block.

    Anchors on the Grand Total line, then skips its own value line if
    separate, then keeps skipping any further "Label \\n $Amount" pairs
    (e.g. a return/refund order's "Refund Total" row) — such a row is
    distinguished from a real item name because its price follows
    *immediately*, with no Sold-by/Supplied-by/Return-window line between,
    unlike every real item.
    """
    grand_idx = next(
        (i for i, line in enumerate(lines) if _GRAND_TOTAL_LINE_RE.match(line.strip())), None
    )
    idx = (grand_idx + 1) if grand_idx is not None else 0

    idx = _next_non_blank(lines, idx)
    if idx < len(lines) and _BARE_PRICE_RE.match(lines[idx].strip()):
        idx += 1

    n = len(lines)
    while True:
        label_idx = _next_non_blank(lines, idx)
        if label_idx >= n:
            break
        label_line = lines[label_idx].strip()
        price_idx = _next_non_blank(lines, label_idx + 1)
        if (
            price_idx < n
            and _BARE_PRICE_RE.match(lines[price_idx].strip())
            and not _BARE_PRICE_RE.match(label_line)
            and not _BARE_QTY_RE.match(label_line)
        ):
            idx = price_idx + 1
        else:
            idx = label_idx
            break
    return idx


def _extract_items(lines: list[str]) -> list[LineItem]:
    items: list[LineItem] = []
    pending_qty = 1
    pending_name: str | None = None
    n = len(lines)
    i = 0

    while i < n:
        line = lines[i].strip()
        i += 1
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

        if _is_skip_line(line):
            continue

        candidate = _item_name_from_line(line)

        # Look ahead for the "truncated preview + glued quantity" pattern:
        # a candidate name line immediately followed by another candidate
        # name line, where stripping trailing digits from this one makes it
        # a prefix of the next. If found, this line is noise (an image-alt
        # preview) — its digits are the quantity, and the real name comes
        # from the next line (which will become pending_name naturally on
        # a later iteration).
        j = _next_non_blank(lines, i)
        if j < n and _is_candidate_name_line(lines[j].strip()):
            digits_match = _TRAILING_DIGITS_RE.match(candidate)
            if digits_match:
                prefix, qty_str = digits_match.groups()
                next_name = _item_name_from_line(lines[j].strip())
                if next_name.startswith(prefix):
                    pending_qty = int(qty_str)
                    continue

        # Any other non-skip line becomes the current candidate name,
        # overwriting whatever came before — delivery/return status prose
        # of arbitrary wording gets superseded once the real item name line
        # is reached, rather than needing to be recognized and skipped by
        # name.
        pending_name = candidate

    return items


def parse_invoice_text(text: str) -> ParsedAmazonOrder | None:
    """Public entry point. Returns None (rather than raising) on any anchor-
    not-found or reconciliation failure, so callers can report a clean
    per-file error instead of crashing an entire batch import."""
    if not text or not text.strip():
        return None

    order_id = _find_order_id(text)
    if not order_id:
        return None

    date_str = _find_order_date_str(text)
    if not date_str:
        return None
    try:
        order_date = _parse_amazon_date(date_str)
    except ValueError:
        return None

    subtotal_str = _find_total(text, r"Item\(s\) Subtotal")
    subtotal = _amount_to_milliunits(subtotal_str) if subtotal_str else None
    if subtotal is None:
        return None

    tax_str = _find_total(text, "Estimated tax to be collected")
    tax = _amount_to_milliunits(tax_str) if tax_str else None
    if tax is None:
        return None

    total_str = _find_total(text, "Grand Total")
    total = _amount_to_milliunits(total_str) if total_str else None
    if total is None:
        return None

    shipping_str = _find_total(text, "Shipping & Handling")
    shipping_and_handling = _amount_to_milliunits(shipping_str) if shipping_str else 0
    free_shipping_str = _find_total(text, "Free Shipping")
    free_shipping = _amount_to_milliunits(free_shipping_str) if free_shipping_str else 0
    shipping = (shipping_and_handling or 0) + (free_shipping or 0)

    lines = text.splitlines()
    item_lines = lines[_find_item_scan_start(lines) :]
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
