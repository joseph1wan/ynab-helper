from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from ynab_helper.models import LineItem, TargetOrder

ORDER_HISTORY_URL = "https://www.target.com/orders"


def _build_browser_launch_kwargs(
    headless: bool, profile_root: Path | None = None
) -> dict[str, Any]:
    resolved_profile_root = profile_root or Path.home() / ".config" / "google-chrome"
    return {
        "headless": headless,
        "channel": "chrome",
        "args": [
            f"--user-data-dir={resolved_profile_root}",
            "--profile-directory=Default",
        ],
    }


def _parse_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return date.fromisoformat(value[:10])


def _to_milliunits(value: float | int | str) -> int:
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.]", "", value)
        value = float(cleaned) if cleaned else 0.0
    if isinstance(value, float):
        return int(round(value * 1000))
    if value > 100000:
        return int(value)
    return int(value * 1000)


def _extract_orders_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("orders", "order_history", "items", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_orders_from_payload(value)
            if nested:
                return nested
    return []


def _parse_line_items(raw_items: list[Any]) -> list[LineItem]:
    items: list[LineItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = (
            raw.get("item_name")
            or raw.get("description")
            or raw.get("title")
            or raw.get("name")
            or "Unknown item"
        )
        quantity = int(raw.get("quantity") or raw.get("qty") or 1)
        price = (
            raw.get("line_total")
            or raw.get("total_price")
            or raw.get("price")
            or raw.get("unit_price")
            or 0
        )
        items.append(
            LineItem(
                name=str(name),
                quantity=quantity,
                line_total=_to_milliunits(price),
            )
        )
    return items


def parse_target_order(raw: dict[str, Any]) -> TargetOrder | None:
    order_id = str(
        raw.get("order_id")
        or raw.get("orderNumber")
        or raw.get("id")
        or raw.get("order_number")
        or ""
    )
    if not order_id:
        return None

    date_raw = (
        raw.get("order_date")
        or raw.get("orderDate")
        or raw.get("placed_date")
        or raw.get("date")
        or ""
    )
    if not date_raw:
        return None

    line_items_raw = (
        raw.get("line_items")
        or raw.get("items")
        or raw.get("order_lines")
        or raw.get("products")
        or []
    )
    line_items = _parse_line_items(line_items_raw)

    total_raw = (
        raw.get("order_total")
        or raw.get("total")
        or raw.get("grand_total")
        or raw.get("amount")
    )
    if total_raw is None and line_items:
        total_raw = sum(item.line_total for item in line_items)
    if total_raw is None:
        return None

    return TargetOrder(
        order_id=order_id,
        order_date=_parse_date(str(date_raw)),
        total=_to_milliunits(total_raw),
        line_items=line_items,
        tax=_to_milliunits(raw.get("tax") or raw.get("tax_total") or 0),
        shipping=_to_milliunits(
            raw.get("shipping") or raw.get("shipping_total") or 0
        ),
        fees=_to_milliunits(
            raw.get("fees") or raw.get("bag_fee") or raw.get("bagFee") or 0
        ),
    )


def _collect_orders_from_responses(
    responses: list[dict[str, Any]], since_date: date
) -> list[TargetOrder]:
    seen: set[str] = set()
    orders: list[TargetOrder] = []
    for payload in responses:
        for raw in _extract_orders_from_payload(payload):
            order = parse_target_order(raw)
            if not order or order.order_id in seen:
                continue
            if order.order_date < since_date:
                continue
            seen.add(order.order_id)
            orders.append(order)
    return sorted(orders, key=lambda o: o.order_date)


def scrape_target_orders(
    auth_path: Path,
    since_date: date,
    output_dir: Path,
    headless: bool = True,
) -> list[TargetOrder]:
    if not auth_path.exists():
        raise FileNotFoundError(
            f"Target auth not found at {auth_path}. "
            "Run: ynab-helper target-login"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, Any]] = []
    launch_kwargs = _build_browser_launch_kwargs(headless=headless)
    profile_root = Path(launch_kwargs["args"][0].split("=", 1)[1])
    browser_args = [
        arg for arg in launch_kwargs["args"] if not arg.startswith("--user-data-dir=")
    ]

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_root),
            headless=launch_kwargs["headless"],
            channel=launch_kwargs["channel"],
            args=browser_args,
        )
        page = context.new_page()

        def on_response(response: Any) -> None:
            url = response.url.lower()
            if "order" not in url:
                return
            try:
                if "json" not in (response.headers.get("content-type") or ""):
                    return
                body = response.json()
                captured.append(body)
            except Exception:
                return

        page.on("response", on_response)
        page.goto(ORDER_HISTORY_URL, wait_until="networkidle", timeout=60000)

        for _ in range(10):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            load_more = page.locator(
                'button:has-text("Load more"), button:has-text("Show more")'
            )
            if load_more.count() > 0:
                try:
                    load_more.first.click(timeout=2000)
                    page.wait_for_timeout(2000)
                except Exception:
                    break
            else:
                break

        context.close()

    orders = _collect_orders_from_responses(captured, since_date)
    for order in orders:
        out_path = output_dir / f"{order.order_id}.json"
        with out_path.open("w") as f:
            json.dump(
                {
                    "order_id": order.order_id,
                    "order_date": order.order_date.isoformat(),
                    "total": order.total,
                    "tax": order.tax,
                    "shipping": order.shipping,
                    "fees": order.fees,
                    "line_items": [
                        {
                            "name": li.name,
                            "quantity": li.quantity,
                            "line_total": li.line_total,
                        }
                        for li in order.line_items
                    ],
                },
                f,
                indent=2,
            )
    return orders


def load_cached_orders(output_dir: Path, since_date: date) -> list[TargetOrder]:
    if not output_dir.exists():
        return []
    orders: list[TargetOrder] = []
    for path in output_dir.glob("*.json"):
        with path.open() as f:
            raw = json.load(f)
        order = parse_target_order(raw)
        if order and order.order_date >= since_date:
            orders.append(order)
    return sorted(orders, key=lambda o: o.order_date)


def save_target_session(auth_path: Path) -> None:
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    launch_kwargs = _build_browser_launch_kwargs(headless=False)
    profile_root = Path(launch_kwargs["args"][0].split("=", 1)[1])
    browser_args = [
        arg for arg in launch_kwargs["args"] if not arg.startswith("--user-data-dir=")
    ]
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_root),
            headless=launch_kwargs["headless"],
            channel=launch_kwargs["channel"],
            args=browser_args,
        )
        page = context.new_page()
        page.goto("https://www.target.com/login", wait_until="domcontentloaded")
        print("Log in to Target in the browser window, then press Enter here...")
        input()
        context.storage_state(path=str(auth_path))
        context.close()
    print(f"Saved Target session to {auth_path}")
