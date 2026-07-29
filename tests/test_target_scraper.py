from __future__ import annotations

from datetime import date
from pathlib import Path

import ynab_helper.target_scraper as target_scraper
from ynab_helper.target_scraper import _build_browser_launch_kwargs, parse_target_order


def test_build_browser_launch_kwargs_uses_default_chrome_profile(tmp_path: Path) -> None:
    profile_root = tmp_path / "google-chrome"

    kwargs = _build_browser_launch_kwargs(headless=False, profile_root=profile_root)

    assert kwargs["channel"] == "chrome"
    assert kwargs["args"] == [
        f"--user-data-dir={profile_root}",
        "--profile-directory=Default",
    ]


def test_parse_target_order_supports_camel_case_keys() -> None:
    order = parse_target_order(
        {
            "orderId": "abc-123",
            "orderDate": "2026-07-10",
            "orderTotal": "12.345",
            "taxTotal": "0.5",
            "shippingTotal": "1.0",
            "lineItems": [
                {"productName": "Milk", "quantity": 2, "lineTotal": "6.15"},
                {"productName": "Bread", "quantity": 1, "lineTotal": "6.195"},
            ],
        }
    )

    assert order is not None
    assert order.order_id == "abc-123"
    assert order.order_date == date(2026, 7, 10)
    assert order.total == 12345
    assert [item.name for item in order.line_items] == ["Milk", "Bread"]
    assert order.line_items[0].line_total == 6150


def test_parse_invoice_html_line_items() -> None:
    html = """
    <div data-test="invoice-details-card">
      <div class="styles_infoRow__k6eLr">
        <div>
          <p>Item</p>
          <b><p class="h-padding-v-tiny">94844694 - Baby 4pk Moon Short Sleeve Bodysuit - Cloud Island™ Gray 6-9</p></b>
        </div>
        <div class="styles_spaceBetweenDiv__bpE2M">
          <div class="styles_innerDiv__ds__L" data-test="item-quantity"><div>Qty.</div><div><b>1</b></div></div>
          <div class="styles_innerDiv__ds__L">Unit price<b>$12.00</b></div>
          <div class="styles_innerDiv__ds__L">Amount<b>$12.00</b></div>
        </div>
      </div>
    </div>
    """

    items = target_scraper._parse_invoice_html_line_items(html)

    assert len(items) == 1
    assert items[0].name == "Baby 4pk Moon Short Sleeve Bodysuit - Cloud Island™ Gray 6-9"
    assert items[0].quantity == 1
    assert items[0].line_total == 12000


def test_parse_invoice_html_uses_each_rows_own_amount() -> None:
    html = """
    <div data-test="invoice-details-card">
      <div class="styles_infoRow__one"><p>Item</p><b><p>111 - Apples</p></b>
        <div data-test="item-quantity"><div>Qty.</div><div><b>2</b></div></div>
        <div>Amount<b>$3.50</b></div></div>
    </div>
    <div data-test="invoice-details-card">
      <div class="styles_infoRow__two"><p>Item</p><b><p>222 - Bread</p></b>
        <div data-test="item-quantity"><div>Qty.</div><div><b>1</b></div></div>
        <div>Amount<b>$4.25</b></div></div>
    </div>
    """

    items = target_scraper._parse_invoice_html_line_items(html)

    assert [(item.name, item.quantity, item.line_total) for item in items] == [
        ("Apples", 2, 3500),
        ("Bread", 1, 4250),
    ]


def test_pause_for_debug_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr("builtins.input", lambda: calls.append("called") or "")

    target_scraper._pause_for_debug("open Target", enabled=False)
    assert calls == []

    target_scraper._pause_for_debug("open Target", enabled=True)
    assert calls == ["called"]


def test_reached_cutoff_when_loaded_page_contains_an_older_order() -> None:
    responses = [
        {
            "orders": [
                {"orderId": "recent", "orderDate": "2026-07-20", "orderTotal": "1"},
                {"orderId": "older", "orderDate": "2026-07-18", "orderTotal": "1"},
            ]
        }
    ]

    assert target_scraper._reached_cutoff(responses, date(2026, 7, 19))


def test_reached_cutoff_ignores_non_order_responses_and_recent_orders() -> None:
    responses = [
        {"data": {"status": "ok"}},
        {"orders": [{"orderId": "recent", "orderDate": "2026-07-20", "orderTotal": "1"}]},
    ]

    assert not target_scraper._reached_cutoff(responses, date(2026, 7, 19))


def _order(order_id: str) -> target_scraper.TargetOrder:
    from ynab_helper.models import TargetOrder

    return TargetOrder(order_id=order_id, order_date=date(2026, 7, 16), total=1000, line_items=[])


def test_parse_invoices_for_order_skips_html_already_imported_from_paste(tmp_path: Path) -> None:
    # debug/ and pasted/ must be siblings — _parse_invoices_for_order derives
    # pasted_dir from debug_dir.parent / "pasted".
    root = tmp_path / "data" / "target-orders"
    debug_dir = root / "debug"
    pasted_dir = root / "pasted"
    debug_dir.mkdir(parents=True)
    pasted_dir.mkdir(parents=True)

    # A stale HTML capture present alongside a paste for the same invoice id.
    (debug_dir / "invoice_912_555.html").write_text("<html>irrelevant, should never be read</html>")
    (pasted_dir / "invoice_912_555.txt").write_text("already imported")
    # The paste importer already wrote the JSON this invoice belongs to.
    (root / "912_555.json").write_text("{}")

    results = target_scraper._parse_invoices_for_order(_order("912"), debug_dir, output_dir=root)

    assert results == []


def test_parse_invoices_for_order_falls_back_to_bare_order_when_nothing_exists(
    tmp_path: Path,
) -> None:
    debug_dir = tmp_path / "debug"
    debug_dir.mkdir()
    order = _order("912")

    results = target_scraper._parse_invoices_for_order(order, debug_dir)

    assert results == [order]


def test_parse_invoices_for_order_does_not_duplicate_when_invoice_json_exists(
    tmp_path: Path,
) -> None:
    debug_dir = tmp_path / "debug"
    output_dir = tmp_path / "orders"
    debug_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "912_555.json").write_text("{}")

    results = target_scraper._parse_invoices_for_order(
        _order("912"), debug_dir, output_dir=output_dir
    )

    assert results == []
