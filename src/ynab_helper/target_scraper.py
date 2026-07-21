from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def _pause_for_debug(step_name: str, enabled: bool = False) -> None:
    if not enabled:
        return
    print(f"[debug] {step_name} — press Enter to continue...")
    input()


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


def _get_first_value(data: Any, aliases: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        for alias in aliases:
            if alias in data and data[alias] not in (None, ""):
                return data[alias]
        for value in data.values():
            found = _get_first_value(value, aliases)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = _get_first_value(item, aliases)
            if found not in (None, ""):
                return found
    return None


def _extract_orders_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in (
        "orders",
        "order_history",
        "orderHistory",
        "items",
        "results",
        "data",
        "orderHistoryItems",
    ):
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
    if not isinstance(raw_items, list):
        return items

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue

        candidate_names = []
        for key in (
            "item_name",
            "itemName",
            "description",
            "product_name",
            "productName",
            "title",
            "name",
            "displayName",
            "display_name",
            "productDisplayName",
            "productDisplayName",
            "itemDisplayName",
        ):
            if key in raw and raw[key] not in (None, ""):
                candidate_names.append(raw[key])

        nested_name = _get_first_value(
            raw,
            (
                "item_name",
                "itemName",
                "product_name",
                "productName",
                "productDisplayName",
                "itemDisplayName",
                "name",
            ),
        )
        if nested_name not in (None, ""):
            candidate_names.append(nested_name)

        name = None
        for candidate in candidate_names:
            if isinstance(candidate, str) and candidate.strip():
                normalized = candidate.upper()
                if normalized in {
                    "ORDER_PICKED_UP",
                    "DELIVERED",
                    "REFUND_ISSUED",
                    "UNKNOWN ITEM",
                    "UNKNOWN",
                }:
                    continue
                if any(word in normalized for word in ("ITEM", "PRODUCT", "SKU")):
                    continue
                name = candidate
                break
        if not name:
            fallback = _get_first_value(
                raw,
                (
                    "item_name",
                    "itemName",
                    "description",
                    "product_name",
                    "productName",
                    "title",
                    "name",
                    "displayName",
                    "display_name",
                    "productDisplayName",
                    "itemDisplayName",
                ),
            )
            if isinstance(fallback, str) and fallback.strip():
                name = fallback
            else:
                name = "Unknown item"

        quantity = _get_first_value(raw, ("quantity", "qty", "itemQuantity"))
        price = _get_first_value(
            raw,
            (
                "line_total",
                "lineTotal",
                "total_price",
                "totalPrice",
                "price",
                "unit_price",
                "unitPrice",
            ),
        )
        if not price:
            amount_candidates = [
                _get_first_value(raw, ("amount", "unitAmount", "itemAmount")),
                _get_first_value(raw, ("value", "cost", "total")),
            ]
            for amount in amount_candidates:
                if amount not in (None, ""):
                    price = amount
                    break

        items.append(
            LineItem(
                name=str(name or "Unknown item"),
                quantity=int(quantity or 1),
                line_total=_to_milliunits(price or 0),
            )
        )
    return items


def parse_target_order(raw: dict[str, Any]) -> TargetOrder | None:
    order_id = str(
        _get_first_value(
            raw,
            ("order_id", "orderId", "orderNumber", "id", "order_number"),
        )
        or ""
    )
    if not order_id:
        return None

    date_raw = _get_first_value(
        raw,
        (
            "order_date",
            "orderDate",
            "placed_date",
            "placedDate",
            "date",
            "createdDate",
        ),
    )
    if not date_raw:
        return None

    line_items_raw = _get_first_value(
        raw,
        (
            "line_items",
            "lineItems",
            "items",
            "order_lines",
            "orderLines",
            "products",
            "productItems",
        ),
    )
    line_items = _parse_line_items(line_items_raw)

    total_raw = _get_first_value(
        raw,
        (
            "order_total",
            "orderTotal",
            "total",
            "grand_total",
            "grandTotal",
            "amount",
            "orderAmount",
        ),
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
        tax=_to_milliunits(
            _get_first_value(raw, ("tax", "tax_total", "taxTotal")) or 0
        ),
        shipping=_to_milliunits(
            _get_first_value(raw, ("shipping", "shipping_total", "shippingTotal"))
            or 0
        ),
        fees=_to_milliunits(
            _get_first_value(raw, ("fees", "bag_fee", "bagFee", "feeTotal")) or 0
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
    headless: bool = False,
    debug_pause: bool = False,
) -> list[TargetOrder]:
    if not auth_path.exists():
        raise FileNotFoundError(
            f"Target auth not found at {auth_path}. "
            "Run: ynab-helper target-login"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, Any]] = []
    debug_dir = output_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
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

        captcha_detected = False

        def on_response(response: Any) -> None:
            nonlocal captcha_detected
            url = response.url.lower()
            if "captcha" in url:
                captcha_detected = True
                return

            try:
                content_type = response.headers.get("content-type") or ""
                is_json = "json" in content_type.lower()
                if not is_json:
                    return

                body = response.json()
                captured.append(body)
                payload_path = debug_dir / f"response_{len(captured):03d}.json"
                with payload_path.open("w") as f:
                    json.dump(body, f, indent=2)
            except Exception:
                return

        page.on("response", on_response)
        page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)

        _pause_for_debug("after opening Target order history", enabled=debug_pause)

        if not headless:
            print("Solve any Target captcha in the browser, then press Enter here...")
            input()
        else:
            _pause_for_debug("after solving captcha", enabled=debug_pause)

        if captcha_detected and headless:
            raise RuntimeError(
                f"Target blocked the scrape with a captcha challenge: {ORDER_HISTORY_URL}"
            )

        _pause_for_debug("before loading more orders", enabled=debug_pause)
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

        _pause_for_debug("before capturing invoice pages", enabled=debug_pause)
        # Capture invoice pages linked from the orders list so we have the
        # printable HTML available for offline parsing.
        try:
            anchors = page.locator('a[href*="/orders/"][href*="/invoices/"]')
            count = anchors.count()
            for idx in range(count):
                try:
                    href = anchors.nth(idx).get_attribute("href")
                    if not href:
                        continue
                    parsed = urlparse(href)
                    parts = parsed.path.split("/")
                    order_id = parts[2] if len(parts) > 2 else f"order{idx+1}"
                    invoice_id = parts[4] if len(parts) > 4 else f"{idx+1}"
                    full = href if href.startswith("http") else f"https://www.target.com{href}"

                    invoice_path = debug_dir / f"{order_id}_invoice_{invoice_id}.html"
                    new_page = context.new_page()
                    try:
                        new_page.goto(full, wait_until="domcontentloaded", timeout=60000)
                        html = new_page.content()
                        with invoice_path.open("w") as f:
                            f.write(html)
                    except Exception:
                        # ignore individual invoice failures but continue
                        pass
                    finally:
                        try:
                            new_page.close()
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass
        context.close()

    _pause_for_debug("after scraper finishes", enabled=debug_pause)

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
