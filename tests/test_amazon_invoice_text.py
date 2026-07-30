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


def test_arbitrary_delivery_status_prose_does_not_become_item_name() -> None:
    """"Your package was left in the mail room." isn't hardcoded anywhere —
    the item scanner must recover the real title regardless of wording."""
    text = (FIXTURES / "amazon_sample_4_mailroom.txt").read_text()
    order = parse_invoice_text(text)
    assert order is not None
    assert order.order_id == "114-3667658-8385813"
    assert order.total == 41950
    assert len(order.line_items) == 2
    assert order.line_items[0].name.startswith("FEXIA Shelf Liners")
    assert order.line_items[0].line_total == 33990
    assert order.line_items[1].name.startswith("Stainless Steel Metal Ruler")
    assert order.line_items[1].line_total == 4190


def test_another_delivery_status_wording_also_recovers_real_item_name() -> None:
    text = (FIXTURES / "amazon_sample_5_handed_to_receptionist.txt").read_text()
    order = parse_invoice_text(text)
    assert order is not None
    assert order.order_id == "114-3246807-1137017"
    assert order.total == 25970
    assert order.shipping == 0  # $2.99 shipping netted against -$2.99 free shipping
    assert len(order.line_items) == 2
    assert order.line_items[0].name.startswith("FURTALK Baby Sun Hat")
    assert order.line_items[0].line_total == 12990
    assert order.line_items[1].name.startswith("uideazone Baby Boys")
    assert order.line_items[1].line_total == 12980


def test_return_refund_order_skips_refund_total_row_and_status_prose() -> None:
    """A return/refund order has an extra "Refund Total" row after Grand
    Total, plus "Return complete" / "Your return is complete..." /
    "When will I get my refund?" prose before the real item — none of that
    should become a false item or block the real one from being found."""
    text = (FIXTURES / "amazon_sample_6_return_refund.txt").read_text()
    order = parse_invoice_text(text)
    assert order is not None
    assert order.order_id == "114-1256788-9775405"
    assert order.total == 38530  # Grand Total, not Refund Total
    assert len(order.line_items) == 1
    assert order.line_items[0].name.startswith("MLILY Foldable Mattress")
    assert order.line_items[0].line_total == 74990


def test_zero_grand_total_still_parses() -> None:
    """A Grand Total of exactly $0.00 (fully offset by a gift card) must
    still be treated as a found value, not as "missing"."""
    text = (FIXTURES / "amazon_sample_7_zero_grand_total.txt").read_text()
    order = parse_invoice_text(text)
    assert order is not None
    assert order.total == 0
    assert len(order.line_items) == 1
    assert order.line_items[0].line_total == 15990
