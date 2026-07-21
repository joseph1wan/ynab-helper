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
    """

    items = target_scraper._parse_invoice_html_line_items(html)

    assert len(items) == 1
    assert items[0].name == "Baby 4pk Moon Short Sleeve Bodysuit - Cloud Island™ Gray 6-9"
    assert items[0].quantity == 1
    assert items[0].line_total == 12000


def test_pause_for_debug_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr("builtins.input", lambda: calls.append("called") or "")

    target_scraper._pause_for_debug("open Target", enabled=False)
    assert calls == []

    target_scraper._pause_for_debug("open Target", enabled=True)
    assert calls == ["called"]
