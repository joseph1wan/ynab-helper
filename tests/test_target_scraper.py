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


def test_pause_for_debug_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr("builtins.input", lambda: calls.append("called") or "")

    target_scraper._pause_for_debug("open Target", enabled=False)
    assert calls == []

    target_scraper._pause_for_debug("open Target", enabled=True)
    assert calls == ["called"]
