"""Read cached Amazon order JSON files in data/amazon-orders/.

Unlike Target orders (scraped live by target_scraper.py), Amazon orders are
only ever produced by amazon_import.py draining pasted order text.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ynab_helper.models import AmazonOrder, LineItem


def _order_from_json(raw: dict[str, Any]) -> AmazonOrder | None:
    """Load an AmazonOrder from our own saved JSON format.

    Values are already in milliunits — do not pass through _to_milliunits.
    """
    try:
        return AmazonOrder(
            order_id=raw["order_id"],
            order_date=date.fromisoformat(raw["order_date"]),
            total=int(raw["total"]),
            tax=int(raw.get("tax", 0)),
            shipping=int(raw.get("shipping", 0)),
            fees=int(raw.get("fees", 0)),
            line_items=[
                LineItem(
                    name=li["name"],
                    quantity=int(li.get("quantity", 1)),
                    line_total=int(li["line_total"]),
                )
                for li in raw.get("line_items", [])
            ],
        )
    except (KeyError, ValueError):
        return None


def load_cached_amazon_orders(
    output_dir: Path, since_date: date, until_date: date | None = None
) -> list[AmazonOrder]:
    if not output_dir.exists():
        return []
    orders: list[AmazonOrder] = []
    for path in output_dir.glob("*.json"):
        with path.open() as f:
            raw = json.load(f)
        order = _order_from_json(raw)
        if order is None or order.order_date < since_date:
            continue
        if until_date is not None and order.order_date > until_date:
            continue
        orders.append(order)
    return sorted(orders, key=lambda o: o.order_date)
