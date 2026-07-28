from __future__ import annotations

import json
import random
import re
import time
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


def _pause_if_weird(reason: str, headless: bool) -> None:
    """Pause for manual inspection when the page looks off, e.g. an empty
    order list or content that never finished rendering — the symptoms we've
    seen from Target serving a degraded/stuck response to automated traffic.

    Always fires (unlike --debug-pause, which is opt-in for routine steps)
    because these are specifically the moments worth a human look. No-op
    when headless, since there's no visible window and no one to press Enter.
    """
    if headless:
        print(f"[warning] {reason} (headless — continuing without pause)")
        return
    print(f"[warning] {reason} — inspect the browser window, then press Enter to continue...")
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

    # Each product is wrapped in a data-test="invoice-details-card" div.
    # We scope all extraction to that card so discount/subtotal rows below the
    # infoRow cannot bleed into a sibling card's Amount match.
    card_matches = re.finditer(
        r'data-test="invoice-details-card"[^>]*>(.*?)(?=data-test="invoice-details-card"|$)',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for card_match in card_matches:
        card_html = card_match.group(1)

        # Product name is in the infoRow, inside <b><p>...</p></b>
        name_match = re.search(
            r'styles_infoRow[^>]*>.*?<b>\s*<p[^>]*>(.*?)</p>\s*</b>',
            card_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not name_match:
            continue
        raw_name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", name_match.group(1))).strip()
        # Strip leading TCIN prefix like "94924105 - "
        name = re.sub(r"^\d+\s*-\s*", "", raw_name).strip()
        if not name:
            continue

        # Qty is inside data-test="item-quantity": <div>Qty.</div><div><b>N</b></div>
        qty_match = re.search(
            r'data-test="item-quantity"[^>]*>.*?<b>(\d+)</b>',
            card_html,
            re.IGNORECASE | re.DOTALL,
        )
        quantity = int(qty_match.group(1)) if qty_match else 1

        # Amount (qty × unit price, pre-discount) is the last innerDiv in infoRow.
        # Scope the search to just the infoRow so discount rows outside it are ignored.
        inforow_match = re.search(
            r'styles_infoRow[^>]*>(.*?)</div>\s*</div>\s*</div>',
            card_html,
            re.IGNORECASE | re.DOTALL,
        )
        inforow_html = inforow_match.group(1) if inforow_match else card_html
        amount_match = re.search(
            r"Amount\s*<b>\$(\d+(?:\.\d{1,2})?)</b>",
            inforow_html,
            re.IGNORECASE | re.DOTALL,
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


def _parse_invoice_html_total(html: str) -> int | None:
    """Extract the 'Invoice total' from a Target invoice page (milliunits)."""
    text = unescape(html)
    match = re.search(
        r"Invoice\s+total\s*</?\w[^>]*>.*?<b>\$(\d+(?:\.\d{1,2})?)</b>",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return _to_milliunits(match.group(1))


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
            # For store pickup orders, Target API nests the real product info
            # under raw["item"] while raw["grouping"]["name"] = "ORDER_PICKED_UP".
            # Check raw["item"] explicitly before the general recursive search,
            # otherwise _get_first_value recurses into grouping first and returns
            # ORDER_PICKED_UP (dict insertion order puts grouping before item).
            item_sub = raw.get("item") if isinstance(raw, dict) else None
            if isinstance(item_sub, dict):
                for key in ("description", "item_name", "itemName", "product_name", "title", "name"):
                    val = item_sub.get(key)
                    if isinstance(val, str) and val.strip():
                        name = val.strip()
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


def _make_error_logger(debug_dir: Path) -> Any:
    import traceback
    from datetime import datetime as _dt

    error_log = debug_dir / "fetch_errors.log"

    def _log_error(order_id: str, invoice_id: str | None, exc: Exception) -> None:
        ts = _dt.now().isoformat(timespec="seconds")
        subject = f"order {order_id}" + (f" invoice {invoice_id}" if invoice_id else "")
        msg = f"[{ts}] FAILED {subject}: {type(exc).__name__}: {exc}\n"
        msg += "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with error_log.open("a") as fh:
            fh.write(msg + "\n")
        print(f"Could not capture {subject}: {exc}")

    return _log_error


def _needs_invoice_capture(
    order_id: str, debug_dir: Path, overwrite: bool = False
) -> bool:
    """True if we should open this order's detail page to capture invoices."""
    if overwrite:
        return True
    detail_path = debug_dir / f"order_{order_id}.html"
    if not detail_path.exists():
        return True
    # Parse the saved detail page for invoice links we already know about.
    html = detail_path.read_text(encoding="utf-8", errors="ignore")
    invoice_ids = list(dict.fromkeys(re.findall(
        r'/orders/[^/]+/invoices/(\d+)', html
    )))
    if not invoice_ids:
        # Detail already visited and confirmed no invoices (pickup / pending order).
        return False
    return any(
        not (debug_dir / f"invoice_{order_id}_{inv_id}.html").exists()
        for inv_id in invoice_ids
    )


def _capture_invoices_for_order(
    context: Any,
    order_id: str,
    order_href: str,
    debug_dir: Path,
    log_error: Any,
    ensure_captcha_clearance: Any | None = None,
    debug_pause: bool = False,
    overwrite: bool = False,
    headless: bool = False,
) -> None:
    """Open the order detail in a new tab, then capture each invoice in its own tab.

    Never navigates the main order-history page, so the pagination state is preserved.
    """
    detail_page = context.new_page()
    try:
        detail_page.goto(
            f"https://www.target.com{order_href}",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        detail_page.wait_for_load_state("domcontentloaded", timeout=10000)
        if ensure_captcha_clearance:
            ensure_captcha_clearance(detail_page)
        try:
            detail_page.locator(INVOICE_LINK_SELECTOR).first.wait_for(
                state="attached", timeout=3000
            )
        except Exception:
            pass

        detail_path = debug_dir / f"order_{order_id}.html"
        detail_path.write_text(detail_page.content(), encoding="utf-8")

        invoice_hrefs: list[str] = list(
            dict.fromkeys(
                href
                for i in range(detail_page.locator('a[href*="/invoices/"]').count())
                if (href := detail_page.locator('a[href*="/invoices/"]').nth(i).get_attribute("href"))
            )
        )
        print(f"  {order_id}: {len(invoice_hrefs)} invoice link(s) found")

        if not invoice_hrefs:
            print(f"  {order_id}: no invoice links — detail saved to {detail_path.name}")
            return

        for invoice_href in invoice_hrefs:
            invoice_id = invoice_href.rstrip("/").split("/")[-1]
            invoice_path = debug_dir / f"invoice_{order_id}_{invoice_id}.html"
            if invoice_path.exists() and not overwrite:
                print(f"  {order_id}/{invoice_id}: already captured, skipping")
                continue

            # Throttle between invoice captures to look like a person
            # clicking through order history, not a bot — rapid back-to-back
            # new-tab goto()s to invoice URLs were getting served an infinite
            # loading skeleton (no invoice-details-card ever renders).
            time.sleep(random.uniform(4.0, 8.0))

            # Re-establish the order-detail page as the referrer before each
            # click, since the previous iteration navigated this tab away.
            detail_page.goto(
                f"https://www.target.com{order_href}",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            if ensure_captcha_clearance:
                ensure_captcha_clearance(detail_page)

            # Pause as if scanning the order-detail page before clicking —
            # an instant click right after page load is itself a bot tell.
            time.sleep(random.uniform(1.5, 3.5))

            try:
                link = detail_page.locator(f'a[href="{invoice_href}"]').first
                link.wait_for(state="attached", timeout=5000)
                link.click()
                detail_page.wait_for_load_state("domcontentloaded", timeout=60000)
                if ensure_captcha_clearance:
                    ensure_captcha_clearance(detail_page)
                try:
                    detail_page.wait_for_selector(
                        '[data-test="invoice-details-card"]', timeout=15000
                    )
                except Exception:
                    _pause_if_weird(
                        f"{order_id}/{invoice_id}: invoice-details-card never "
                        "rendered — Target may be serving a stuck/degraded page",
                        headless=headless,
                    )
                else:
                    # Pause as if actually reading the invoice before moving on.
                    time.sleep(random.uniform(2.0, 4.5))
                invoice_path.write_text(detail_page.content(), encoding="utf-8")
                print(f"  {order_id}/{invoice_id}: saved to {invoice_path.name}")
            except Exception as exc:
                log_error(order_id, invoice_id, exc)

        _pause_for_debug(f"after invoices for {order_id}", enabled=debug_pause)
    except Exception as exc:
        log_error(order_id, None, exc)
    finally:
        detail_page.close()


def _parse_invoices_for_order(
    order: TargetOrder, debug_dir: Path, output_dir: Path | None = None
) -> list[TargetOrder]:
    """Return one TargetOrder per invoice found for this order.

    Each invoice maps to its own YNAB charge (separate bank posting).
    A manually pasted invoice (data/target-orders/pasted/invoice_{order}_{invoice}.txt,
    see invoice_import.py) takes precedence over scraped HTML for the same
    invoice id, since it's human-verified and immune to the hydration/$0
    failures a rushed scrape can produce — so it's skipped here entirely,
    letting the already-imported JSON stand.

    Falls back to the original bare order only if no invoice files exist at
    all *and* an invoice-keyed record for this order isn't already on disk
    (output_dir) — otherwise a bare {order_id}.json would sit alongside a
    good {order_id}_{invoice_id}.json and double-match at propose time.
    """
    pasted_dir = debug_dir.parent / "pasted"
    invoice_files = sorted(debug_dir.glob(f"invoice_{order.order_id}_*.html"))

    results: list[TargetOrder] = []
    for invoice_path in invoice_files:
        # invoice_{order_id}_{invoice_id}.html → invoice_id is the last segment
        invoice_id = invoice_path.stem.split("_", 2)[-1]

        if (pasted_dir / f"invoice_{order.order_id}_{invoice_id}.txt").exists():
            # Already imported from a paste; that JSON takes precedence.
            continue

        html = invoice_path.read_text(encoding="utf-8", errors="ignore")
        items = _parse_invoice_html_line_items(html)
        if not items:
            continue
        total = _parse_invoice_html_total(html) or sum(li.line_total for li in items)
        results.append(
            TargetOrder(
                order_id=f"{order.order_id}_{invoice_id}",
                order_date=order.order_date,
                total=total,
                line_items=items,
            )
        )

    if results:
        return results

    if output_dir is not None and list(output_dir.glob(f"{order.order_id}_*.json")):
        # An invoice-keyed record for this order already exists (from a
        # paste import or an earlier successful scrape) — don't also emit
        # a bare duplicate.
        return []

    return [order]


def _collect_orders_from_responses(
    responses: list[dict[str, Any]],
    since_date: date,
    debug_dir: Path | None = None,
    output_dir: Path | None = None,
) -> list[TargetOrder]:
    seen_order_ids: set[str] = set()
    orders: list[TargetOrder] = []
    for payload in responses:
        for raw in _extract_orders_from_payload(payload):
            order = parse_target_order(raw)
            if not order or order.order_id in seen_order_ids:
                continue
            if order.order_date < since_date:
                continue
            seen_order_ids.add(order.order_id)
            if debug_dir is not None:
                orders.extend(_parse_invoices_for_order(order, debug_dir, output_dir=output_dir))
            else:
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
    overwrite: bool = False,
) -> list[TargetOrder]:
    if headless:
        # Headless runs can't show a captcha challenge or a _pause_if_weird
        # prompt for manual inspection — both silently no-op, which is how
        # a soft-blocked/degraded scrape ends up looking like a normal
        # success (see: the "saved 9 orders" incident). Always run headed.
        raise ValueError(
            "Headless scraping is disabled — Target's soft-blocking is only "
            "visible/recoverable in a headed browser. Run with a visible window."
        )
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

        log_error = _make_error_logger(debug_dir)

        page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
        _pause_for_debug("after opening Target order history", enabled=debug_pause)
        ensure_captcha_clearance(page)

        ORDER_LINK_SELECTOR = (
            'a[data-test="order-details-link"] a[href^="/orders/"], '
            'a[href^="/orders/"]:has-text("View purchase")'
        )
        seen_order_ids: set[str] = set()

        for batch_number in range(1, 11):
            ensure_captcha_clearance(page)

            # Collect all order hrefs visible on the page right now.
            order_links = page.locator(ORDER_LINK_SELECTOR)
            batch_hrefs = list(dict.fromkeys(
                href
                for idx in range(order_links.count())
                if (href := order_links.nth(idx).get_attribute("href"))
            ))
            new_hrefs = [
                h for h in batch_hrefs
                if h.rstrip("/").split("/")[-1] not in seen_order_ids
            ]
            print(f"Batch {batch_number}: {len(new_hrefs)} new order(s) to process")

            if not batch_hrefs:
                _pause_if_weird(
                    f"Batch {batch_number}: 0 order links matched "
                    f"{ORDER_LINK_SELECTOR!r} — page may be blocked, "
                    "degraded, or its markup changed",
                    headless=headless,
                )

            for order_href in new_hrefs:
                order_id = order_href.rstrip("/").split("/")[-1]
                seen_order_ids.add(order_id)
                ensure_captcha_clearance(page)

                if not _needs_invoice_capture(order_id, debug_dir, overwrite=overwrite):
                    print(f"  {order_id}: all invoices already captured, skipping")
                    continue

                _capture_invoices_for_order(
                    context,
                    order_id,
                    order_href,
                    debug_dir,
                    log_error,
                    ensure_captcha_clearance=ensure_captcha_clearance,
                    debug_pause=debug_pause,
                    overwrite=overwrite,
                    headless=headless,
                )

                # Pause between orders too, not just between invoices within
                # the same order — otherwise the order-to-order handoff is
                # still an instant, bot-paced transition.
                time.sleep(random.uniform(2.5, 5.5))

            if _reached_cutoff(captured, since_date):
                print(f"Reached date cutoff ({since_date}) after batch {batch_number}")
                break

            _pause_for_debug(f"before loading batch {batch_number + 1}", enabled=debug_pause)
            captured_before = len(captured)
            # Scroll gradually rather than jumping straight to the bottom —
            # closer to how a person actually scans down a page.
            for _ in range(4):
                page.mouse.wheel(0, random.randint(300, 600))
                page.wait_for_timeout(random.randint(300, 700))
            page.wait_for_timeout(random.randint(1500, 3000))
            if _reached_cutoff(captured[captured_before:], since_date):
                break

            load_more = page.locator('button:has-text("Load more"), button:has-text("Show more")')
            if load_more.count() > 0:
                try:
                    time.sleep(random.uniform(1.0, 2.5))
                    load_more.first.click(timeout=2000)
                    page.wait_for_timeout(random.randint(2000, 4000))
                except Exception:
                    break
            else:
                break

            if _reached_cutoff(captured[captured_before:], since_date):
                break

        _pause_for_debug("all batches done (Chrome will close after you continue)", enabled=debug_pause)
        context.close()

    orders = _collect_orders_from_responses(
        captured, since_date, debug_dir=debug_dir, output_dir=output_dir
    )
    for order in orders:
        # order.order_id is "{order_id}_{invoice_id}" for invoice-based orders,
        # or just "{order_id}" for orders with no invoice HTML captured yet.
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


def _order_from_json(raw: dict[str, Any]) -> TargetOrder | None:
    """Load a TargetOrder from our own saved JSON format.

    Values are already in milliunits — do not pass through _to_milliunits.
    """
    try:
        return TargetOrder(
            order_id=raw["order_id"],
            order_date=_parse_date(raw["order_date"]),
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


def load_cached_orders(
    output_dir: Path, since_date: date, until_date: date | None = None
) -> list[TargetOrder]:
    if not output_dir.exists():
        return []
    orders: list[TargetOrder] = []
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
