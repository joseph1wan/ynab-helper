from __future__ import annotations

from datetime import date
from pathlib import Path

from ynab_helper.amazon_invoice_text import parse_invoice_text

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_sample_1_single_qty_items_with_free_shipping() -> None:
    text = (FIXTURES / "amazon_sample_1.txt").read_text()
    order = parse_invoice_text(text)
    assert order is not None
    assert order.order_id == "111-1239029-5887460"
    assert order.order_date == date(2026, 7, 24)
    assert order.total == 30730
    assert order.tax == 2010
    assert order.shipping == 0  # $2.99 shipping netted against -$2.99 free shipping
    assert len(order.line_items) == 3
    assert order.line_items[0].name.startswith("Granicell")
    assert order.line_items[0].quantity == 1
    assert order.line_items[0].line_total == 6780
    assert order.line_items[1].line_total == 11970
    assert order.line_items[2].line_total == 9970


def test_parses_sample_2_multi_qty_item_ignores_gift_card_row() -> None:
    text = (FIXTURES / "amazon_sample_2.txt").read_text()
    order = parse_invoice_text(text)
    assert order is not None
    assert order.order_id == "114-2174468-2163434"
    assert order.order_date == date(2026, 6, 21)
    assert order.total == 460  # Grand Total after gift card, read directly
    assert order.tax == 1540
    assert len(order.line_items) == 1
    item = order.line_items[0]
    assert item.quantity == 2
    assert item.line_total == 15380  # unit price $7.69 * quantity 2


def test_missing_order_id_returns_none() -> None:
    text = (FIXTURES / "amazon_sample_1.txt").read_text().replace("Order #", "Order id")
    assert parse_invoice_text(text) is None


def test_missing_grand_total_returns_none() -> None:
    text = (FIXTURES / "amazon_sample_1.txt").read_text().replace("* Grand Total:", "* Some Total:")
    assert parse_invoice_text(text) is None


def test_reconciliation_mismatch_returns_none() -> None:
    text = (FIXTURES / "amazon_sample_1.txt").read_text().replace("$6.78", "$99.99")
    assert parse_invoice_text(text) is None


def test_unrecognized_total_label_does_not_break_parsing() -> None:
    text = (FIXTURES / "amazon_sample_1.txt").read_text().replace(
        "* Grand Total:\n$30.73",
        "* Some New Discount:\n-$1.00\n* Grand Total:\n$30.73",
    )
    order = parse_invoice_text(text)
    assert order is not None
    assert order.total == 30730


def test_empty_text_returns_none() -> None:
    assert parse_invoice_text("") is None
    assert parse_invoice_text("   \n  ") is None


def test_parses_real_clipboard_paste_with_collapsed_header_and_glued_qty() -> None:
    """Regression test for a real (non-fabricated) clipboard paste, where:
    - "Order placed" and "Order #" land on the same line
    - total labels have no "* " bullet prefix
    - the quantity (2) is glued directly onto a truncated preview line
      ("...Storage Bin2") immediately before the item's full title line
    """
    text = (FIXTURES / "amazon_sample_3_real_paste.txt").read_text()
    order = parse_invoice_text(text)
    assert order is not None
    assert order.order_id == "114-2174468-2163434"
    assert order.order_date == date(2026, 6, 21)
    assert order.total == 460
    assert order.tax == 1540
    assert len(order.line_items) == 1
    item = order.line_items[0]
    assert item.name == "iDesign Slim Extra Long Clear Storage Bin, Narrow Stackable Organizer for Kitchen or Pantry"
    assert item.quantity == 2
    assert item.line_total == 15380
