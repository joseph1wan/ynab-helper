from __future__ import annotations

import json
import re
from datetime import date, datetime
from html import unescape
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from ynab_helper.models import LineItem, TargetOrder

ORDER_HISTORY_URL = "https://www.target.com/orders"
INVOICE_LINK_SELECTOR = 'a[href*="invoice" i], a[href*="receipt" i]'
INVOICE_BUTTON_NAME = re.compile(
    r"(?:view |download |print )?(?:invoice|receipt)", re.I
)


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


def _is_auth_interstitial(page: Page) -> bool:
    try:
        if "/login" in page.url:
            return True
        body_text = page.locator("body").inner_text(timeout=1000).lower()
        return any(
            phrase in body_text
            for phrase in (
                "sign in to your account",
                "verify you are human",
                "security check",
            )
        )
    except Exception:
        return False


def _wait_for_captcha_clearance(page: Page, detected: bool) -> None:
    if not detected and not _is_auth_interstitial(page):
        return

    print("Target sign-in challenge detected. Sign in in Chrome, then press Enter to continue...")
    input()
    # Let Target complete the post-captcha/login navigation before the next
    # capture or selector lookup.
    page.wait_for_timeout(750)
    if _is_auth_interstitial(page):
        raise RuntimeError("Target sign-in page is still present after confirmation")


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


def _parse_invoice_html_line_items(html: str) -> list[LineItem]:
    items: list[LineItem] = []
    if not html:
        return items

    text = unescape(html)
    row_matches = re.finditer(
        r'<div[^>]*class="[^"]*styles_infoRow[^"]*"[^>]*>(.*?)(?=<div[^>]*class="[^"]*styles_infoRow|$)',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for row_match in row_matches:
        row_html = row_match.group(1)
        paragraphs = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()
            for match in re.finditer(r"<p[^>]*>(.*?)</p>", row_html, re.IGNORECASE | re.DOTALL)
        ]
        names = [
            value
            for value in paragraphs
            if value and value.lower() not in {"item", "qty.", "unit price", "amount"}
        ]
        if not names:
            continue

        name = re.sub(r"^\d+\s*-\s*", "", names[0]).strip()
        quantity_match = re.search(r"qty\.\s*</?[^>]*>?.{0,80}?\b(\d+)\b", row_html, re.IGNORECASE | re.DOTALL)
        quantity = int(quantity_match.group(1)) if quantity_match else 1
        amount_match = re.search(
            r"amount.*?\$(\d+(?:\.\d{1,2})?)", row_html, re.IGNORECASE | re.DOTALL
        )
        if not amount_match:
            continue
        items.append(
            LineItem(
                name=name,
                quantity=quantity,
                line_total=_to_milliunits(amount_match.group(1)),
            )
        )

    return items


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


def _capture_order_invoice_pages(
    page: Page,
    context: Any,
    debug_dir: Path,
    order_history_url: str,
    eligible_order_ids: set[str],
    debug_pause: bool = False,
    ensure_captcha_clearance: Any | None = None,
) -> None:
    """Open each order detail page, then capture its invoice or receipt page."""
    order_links = page.locator(
        'a[data-test="order-details-link"] a[href^="/orders/"], '
        'a[href^="/orders/"]:has-text("View purchase")'
    )
    order_hrefs = [
        href
        for idx in range(order_links.count())
        if (href := order_links.nth(idx).get_attribute("href"))
    ]

    eligible_hrefs = [
        href
        for href in dict.fromkeys(order_hrefs)
        if href.rstrip("/").split("/")[-1] in eligible_order_ids
    ]
    print(f"Opening invoices for {len(eligible_hrefs)} orders in the date window")

    for idx, order_href in enumerate(eligible_hrefs, start=1):
        order_id = order_href.rstrip("/").split("/")[-1]
        try:
            if ensure_captcha_clearance:
                ensure_captcha_clearance(page)
            _pause_for_debug(
                f"before opening order {idx} of {len(eligible_hrefs)} ({order_id})",
                enabled=debug_pause,
            )
            order_link = page.locator(f'a[href="{order_href}"]').first
            order_link.scroll_into_view_if_needed()
            before_pages = list(context.pages)
            order_link.click(timeout=10000)

            target_page = page
            if len(context.pages) > len(before_pages):
                target_page = [p for p in context.pages if p not in before_pages][-1]
            target_page.wait_for_load_state("domcontentloaded", timeout=10000)
            if ensure_captcha_clearance:
                ensure_captcha_clearance(target_page)
            # Wait only for the control we need. Target leaves unrelated
            # placeholder elements in the DOM after order data is available.
            try:
                target_page.locator(INVOICE_LINK_SELECTOR).first.wait_for(
                    state="attached", timeout=3000
                )
            except Exception:
                pass

            detail_path = debug_dir / f"order_{order_id}.html"
            with detail_path.open("w") as f:
                f.write(target_page.content())

            invoice_links = target_page.locator(INVOICE_LINK_SELECTOR)
            invoice_buttons = target_page.get_by_role("button", name=INVOICE_BUTTON_NAME)
            matching_controls = target_page.locator(
                'a, button'
            ).evaluate_all(
                """elements => elements
                    .map(element => ({
                        tag: element.tagName.toLowerCase(),
                        text: (element.innerText || element.getAttribute('aria-label') || '').trim(),
                        href: element.getAttribute('href') || '',
                    }))
                    .filter(element => /invoice|receipt/i.test(`${element.text} ${element.href}`))"""
            )
            print(
                f"Invoice search for {order_id}: selector {INVOICE_LINK_SELECTOR!r} "
                f"matched {invoice_links.count()} link(s); button-name regex "
                f"matched {invoice_buttons.count()} button(s); candidates={matching_controls}"
            )
            if invoice_links.count() > 0:
                invoice_control = invoice_links.first
            elif invoice_buttons.count() > 0:
                invoice_control = invoice_buttons.first
            else:
                print(
                    f"No invoice or receipt control found for order {order_id}; "
                    f"saved rendered detail HTML to {detail_path}"
                )
                _pause_for_debug(
                    f"after invoice search for {order_id} (no control found)",
                    enabled=debug_pause,
                )
                if target_page is not page:
                    target_page.close()
                else:
                    page.goto(order_history_url, wait_until="domcontentloaded", timeout=60000)
                continue

            invoice_href = invoice_control.get_attribute("href") or ""
            invoice_id = invoice_href.rstrip("/").split("/")[-1]
            before_pages = list(context.pages)
            invoice_control.click(timeout=10000)
            page.wait_for_timeout(1500)
            invoice_page = page
            if len(context.pages) > len(before_pages):
                invoice_page = [p for p in context.pages if p not in before_pages][-1]
            invoice_page.wait_for_load_state("domcontentloaded", timeout=10000)
            if ensure_captcha_clearance:
                ensure_captcha_clearance(invoice_page)
            if invoice_href and invoice_page.url.rstrip("/") != (
                f"https://www.target.com{invoice_href}".rstrip("/")
            ):
                invoice_page.goto(
                    f"https://www.target.com{invoice_href}",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            invoice_page.wait_for_load_state("domcontentloaded", timeout=10000)

            invoice_path = debug_dir / f"invoice_{order_id}_{invoice_id}.html"
            with invoice_path.open("w") as f:
                f.write(invoice_page.content())
            print(f"Saved invoice page to {invoice_path}")
            _pause_for_debug(
                f"after invoice search/open for order {idx} of {len(eligible_hrefs)}",
                enabled=debug_pause,
            )

            if invoice_page is not page:
                invoice_page.close()
            if target_page is not page and target_page is not invoice_page:
                target_page.close()
            if page.url != order_history_url:
                page.goto(order_history_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            print(f"Could not capture invoice for order {order_id}")
            if page.url != order_history_url:
                page.goto(order_history_url, wait_until="domcontentloaded", timeout=60000)
            continue


def _parse_invoices_for_order(order: TargetOrder, debug_dir: Path) -> TargetOrder:
    invoice_files = sorted(debug_dir.glob(f"invoice_{order.order_id}_*.html"))
    if not invoice_files:
        return order

    parsed_items: list[LineItem] = []
    for invoice_path in invoice_files:
        html = invoice_path.read_text(encoding="utf-8", errors="ignore")
        parsed_items.extend(_parse_invoice_html_line_items(html))

    if parsed_items:
        order.line_items = parsed_items
        if order.total <= 0 and parsed_items:
            order.total = sum(item.line_total for item in parsed_items)

    return order


def _collect_orders_from_responses(
    responses: list[dict[str, Any]], since_date: date, debug_dir: Path | None = None
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
            if debug_dir is not None:
                order = _parse_invoices_for_order(order, debug_dir)
            seen.add(order.order_id)
            orders.append(order)
    return sorted(orders, key=lambda o: o.order_date)


def _reached_cutoff(responses: list[dict[str, Any]], since_date: date) -> bool:
    """Whether a just-loaded order page reaches beyond the desired window."""
    page_dates = [
        order.order_date
        for payload in responses
        for raw in _extract_orders_from_payload(payload)
        if (order := parse_target_order(raw)) is not None
    ]
    return bool(page_dates) and min(page_dates) < since_date


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

        def on_response(response: Any) -> None:
            url = response.url.lower()
            if "captcha" in url:
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

        def ensure_captcha_clearance(active_page: Page) -> None:
            """Pause for every headed captcha, regardless of debug mode."""
            captcha_present = _is_auth_interstitial(active_page)
            if not captcha_present:
                return
            if headless:
                raise RuntimeError(
                    f"Target blocked the scrape with a captcha challenge: {active_page.url}"
                )
            _wait_for_captcha_clearance(active_page, captcha_present)

        page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)

        _pause_for_debug("after opening Target order history", enabled=debug_pause)

        ensure_captcha_clearance(page)

        _pause_for_debug("before loading more orders", enabled=debug_pause)
        for page_number in range(1, 11):
            ensure_captcha_clearance(page)
            if _reached_cutoff(captured, since_date):
                break

            _pause_for_debug(
                f"before loading Target order-history page {page_number + 1}",
                enabled=debug_pause,
            )
            captured_before_load = len(captured)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            if _reached_cutoff(captured[captured_before_load:], since_date):
                break

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

            if _reached_cutoff(captured[captured_before_load:], since_date):
                break

        _pause_for_debug("before capturing invoice pages", enabled=debug_pause)
        try:
            eligible_order_ids = {
                order.order_id
                for order in _collect_orders_from_responses(captured, since_date)
            }
            _capture_order_invoice_pages(
                page,
                context,
                debug_dir,
                ORDER_HISTORY_URL,
                eligible_order_ids,
                debug_pause=debug_pause,
                ensure_captcha_clearance=ensure_captcha_clearance,
            )
        except Exception:
            pass

        _pause_for_debug(
            "after capturing invoice pages (Chrome will close after you continue)",
            enabled=debug_pause,
        )
        context.close()

    orders = _collect_orders_from_responses(captured, since_date, debug_dir=debug_dir)
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
