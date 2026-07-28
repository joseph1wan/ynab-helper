from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ynab_helper.invoice_import import import_pasted_invoices
from ynab_helper.invoice_text import parse_invoice_text, parsed_invoice_order_date

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parses_single_item_invoice_with_general_ledger_adjustment():
    text = _read("invoice_912003599332151_61973991394658558.txt")
    parsed = parse_invoice_text(text)

    assert parsed is not None
    assert parsed.order_id == "912003599332151"
    assert parsed.invoice_id == "61973991394658558"
    assert parsed.total == 13230
    assert len(parsed.line_items) == 1

    item = parsed.line_items[0]
    assert item.name == "Baby 4pk Moon Short Sleeve Bodysuit - Cloud Island™ Gray 6-9"
    assert item.quantity == 1
    assert item.line_total == 12000

    assert parsed_invoice_order_date(parsed) == date(2026, 7, 16)


def test_parses_multi_item_invoice_and_ignores_payment_lines_below_total():
    text = _read("invoice_912003599332151_61973991383936767.txt")
    parsed = parse_invoice_text(text)

    assert parsed is not None
    assert parsed.order_id == "912003599332151"
    assert parsed.invoice_id == "61973991383936767"
    assert parsed.total == 72810
    # Exactly 5 items — coupon/giftcard rows after "Invoice total" must not
    # be parsed as additional items.
    assert len(parsed.line_items) == 5

    names = [item.name for item in parsed.line_items]
    assert "Paul Mitchell Two Hair Shampoo - 10.14 fl oz: Shine Enhancing, Clarifying, For Oily & All Hair Types, Liquid Form" in names
    assert "200ct 1-Ply Disposable Napkins - Dealworthy™" in names

    napkins = next(li for li in parsed.line_items if "Napkins" in li.name)
    assert napkins.line_total == 1990
    assert napkins.quantity == 1


def test_tcin_prefix_stripped_from_item_names():
    text = _read("invoice_912003599332151_61973991394658558.txt")
    parsed = parse_invoice_text(text)
    assert parsed is not None
    assert not parsed.line_items[0].name[0].isdigit()


def test_malformed_text_returns_none():
    assert parse_invoice_text("") is None
    assert parse_invoice_text("just some random text\nwith no anchors") is None
    assert parse_invoice_text("Orders/\n123/\nInvoices/\n456\n") is None  # no date/items/total


@pytest.fixture
def tmp_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    inbox = tmp_path / "pasted" / "inbox"
    archive = tmp_path / "pasted"
    output = tmp_path
    inbox.mkdir(parents=True)
    return inbox, archive, output


def test_import_writes_order_json_and_archives_source(tmp_dirs):
    inbox, archive, output = tmp_dirs
    dest = inbox / "1.txt"
    dest.write_text(_read("invoice_912003599332151_61973991394658558.txt"), encoding="utf-8")

    report = import_pasted_invoices(inbox_dir=inbox, archive_dir=archive, output_dir=output)

    assert len(report.imported) == 1
    assert not report.failed

    out_path = output / "912003599332151_61973991394658558.json"
    assert out_path.exists()
    assert not dest.exists()
    assert (archive / "invoice_912003599332151_61973991394658558.txt").exists()

    import json

    data = json.loads(out_path.read_text())
    assert data["order_id"] == "912003599332151_61973991394658558"
    assert data["total"] == 13230
    assert len(data["line_items"]) == 1


def test_import_reports_failure_and_leaves_file_in_place(tmp_dirs):
    inbox, archive, output = tmp_dirs
    bad = inbox / "bad.txt"
    bad.write_text("not an invoice", encoding="utf-8")

    report = import_pasted_invoices(inbox_dir=inbox, archive_dir=archive, output_dir=output)

    assert not report.imported
    assert len(report.failed) == 1
    assert bad.exists()
