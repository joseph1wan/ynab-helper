from __future__ import annotations

from datetime import date
from pathlib import Path

from ynab_helper.costco_receipt_text import (
    detect_receipt_type,
    parse_gas_receipt_text,
    parse_receipt_text,
    parse_warehouse_receipt_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_detects_gas_station_receipt_type():
    text = _read("costco_gas_774_2026-07-16_16397.txt")
    assert detect_receipt_type(text) == "gas"


def test_detects_warehouse_receipt_type():
    text = _read("costco_warehouse_774_2026-07-16_439.txt")
    assert detect_receipt_type(text) == "warehouse"


def test_detect_receipt_type_returns_none_for_unrecognized_text():
    assert detect_receipt_type("just some random text\nwith no anchors") is None


def test_parses_gas_station_receipt():
    text = _read("costco_gas_774_2026-07-16_16397.txt")
    parsed = parse_receipt_text(text)

    assert parsed is not None
    assert parsed.receipt_type == "gas"
    assert parsed.store_number == "774"
    assert parsed.transaction_number == "16397"
    assert parsed.receipt_date == date(2026, 7, 16)
    assert parsed.total == 54690
    assert parsed.tax == 0
    assert parsed.receipt_id == "774_2026-07-16_16397"

    assert len(parsed.line_items) == 1
    item = parsed.line_items[0]
    assert item.name == "Costco Gas - Regular"
    assert item.quantity == 1
    assert item.line_total == 54690


def test_parses_warehouse_receipt_nets_discounts():
    text = _read("costco_warehouse_774_2026-07-16_439.txt")
    parsed = parse_receipt_text(text)

    assert parsed is not None
    assert parsed.receipt_type == "warehouse"
    assert parsed.store_number == "774"
    assert parsed.transaction_number == "439"
    assert parsed.receipt_date == date(2026, 7, 16)
    assert parsed.total == 81950
    assert parsed.tax == 4300
    assert parsed.receipt_id == "774_2026-07-16_439"

    assert len(parsed.line_items) == 5
    by_name = {item.name: item for item in parsed.line_items}

    # 21.39 - 5.00 discount = 16.39
    assert by_name["KLNX ULTR"].line_total == 16390
    # 10.49 - 2.50 discount = 7.99
    assert by_name["12 OZ BOWL"].line_total == 7990
    # no discount applied
    assert by_name["KS TOWEL"].line_total == 20790
    assert by_name["KS ORG STOCK"].line_total == 9490
    assert by_name["OTTAVIO EVOO"].line_total == 22990

    # discount-netted line items must sum to SUBTOTAL (77.65)
    assert sum(item.line_total for item in parsed.line_items) == 77650


def test_parse_receipt_text_returns_none_for_unrecognized_text():
    assert parse_receipt_text("") is None
    assert parse_receipt_text("just some random text\nwith no anchors") is None


def test_gas_parser_returns_none_without_total():
    text = "Gas Station Receipt\nLake In The Hills #774\nMember\n111948273562\nInvoice#\n16397\nDate:\n07/16/26\n"
    assert parse_gas_receipt_text(text) is None


def test_warehouse_parser_bails_when_subtotal_mismatches_line_items():
    text = _read("costco_warehouse_774_2026-07-16_439.txt")
    # Corrupt one item's price so the sum no longer reconciles with SUBTOTAL.
    corrupted = text.replace("221663\t**KLNX ULTR*\t21.39 Y", "221663\t**KLNX ULTR*\t99.99 Y")
    assert parse_warehouse_receipt_text(corrupted) is None
