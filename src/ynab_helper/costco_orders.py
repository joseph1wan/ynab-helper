"""Read cached Costco receipt JSON files in data/costco-orders/.

Unlike Target orders (scraped live by target_scraper.py), Costco orders are
only ever produced by costco_import.py draining pasted receipt text.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ynab_helper.models import CostcoOrder, LineItem


def _order_from_json(raw: dict[str, Any]) -> CostcoOrder | None:
    """Load a CostcoOrder from our own saved JSON format.

    Values are already in milliunits — do not pass through _to_milliunits.
    """
    try:
        return CostcoOrder(
            receipt_id=raw["receipt_id"],
            receipt_date=date.fromisoformat(raw["receipt_date"]),
            total=int(raw["total"]),
            tax=int(raw.get("tax", 0)),
            shipping=int(raw.get("shipping", 0)),
            fees=int(raw.get("fees", 0)),
            receipt_type=raw.get("receipt_type", "warehouse"),
            store_number=raw.get("store_number", ""),
            transaction_number=raw.get("transaction_number", ""),
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


def load_cached_costco_orders(
    output_dir: Path, since_date: date, until_date: date | None = None
) -> list[CostcoOrder]:
    if not output_dir.exists():
        return []
    orders: list[CostcoOrder] = []
    for path in output_dir.glob("*.json"):
        with path.open() as f:
            raw = json.load(f)
        order = _order_from_json(raw)
        if order is None or order.receipt_date < since_date:
            continue
        if until_date is not None and order.receipt_date > until_date:
            continue
        orders.append(order)
    return sorted(orders, key=lambda o: o.receipt_date)
