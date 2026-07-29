from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import click
import uvicorn

from ynab_helper.config import CONFIG_DIR, load_config, resolve_path
from ynab_helper.costco_fetch import run_costco_propose
from ynab_helper.fetch import run_fetch, run_propose
from ynab_helper.import_dispatch import import_inbox
from ynab_helper.paypal_review import build_paypal_review, reapply_paypal_rules
from ynab_helper.target_scraper import load_cached_orders, save_target_session
from ynab_helper.undo import undo_last
from ynab_helper.ynab_client import YnabClient


def _load_dotenv() -> None:
    env_path = resolve_path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@click.group()
def main() -> None:
    """Match Target orders to YNAB transactions and propose split categorizations."""
    _load_dotenv()


@main.command("sync-categories")
def sync_categories() -> None:
    """Pull YNAB category list into config/categories.json."""
    config = load_config()
    token = config.get("ynab_token", "")
    if not token:
        raise click.ClickException("YNAB_TOKEN not set")

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        categories = client.list_categories()

    out_path = CONFIG_DIR / "categories.json"
    with out_path.open("w") as f:
        json.dump(categories, f, indent=2, sort_keys=True)

    click.echo(f"Wrote {len(categories)} categories to {out_path}")


@main.command("fetch")
@click.option("--since", "since_str", default=None, help="Override start date YYYY-MM-DD")
@click.option("--until", "until_str", default=None, help="Only keep orders on or before YYYY-MM-DD")
@click.option(
    "--overwrite",
    is_flag=True,
    help="Ignore the last successful fetch and re-scrape from --since/bootstrap date",
)
@click.option("--skip-scrape", is_flag=True, help="Use cached Target orders only")
@click.option("--debug-pause", is_flag=True, help="Pause after each scraper step until Enter is pressed")
def fetch_cmd(
    since_str: str | None,
    until_str: str | None,
    overwrite: bool,
    skip_scrape: bool,
    debug_pause: bool,
) -> None:
    """Scrape Target orders and save them locally.

    Always runs with a visible browser window — headless scraping is
    disabled since captcha challenges and degraded/stuck pages are only
    recoverable with the window visible.
    """
    since_override = date.fromisoformat(since_str) if since_str else None
    until_override = date.fromisoformat(until_str) if until_str else None
    result = run_fetch(
        since_override=since_override,
        until_override=until_override,
        overwrite=overwrite,
        skip_scrape=skip_scrape,
        headless=False,
        debug_pause=debug_pause,
    )
    until_note = f" through {until_override}" if until_override else ""
    click.echo(
        f"Fetched since {result.since_date}{until_note}: "
        f"saved {len(result.orders)} Target orders"
    )


@main.command("import-invoices")
@click.argument("files", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--keep",
    is_flag=True,
    help="Parse without moving source files out of the inbox (or wherever FILES live)",
)
def import_invoices_cmd(files: tuple[Path, ...], keep: bool) -> None:
    """Drain inbox/ and dispatch each file to the right parser by filename.

    Workflow: paste a Target invoice to inbox/target_N.txt (pb_target),
    paste a Costco receipt to inbox/costco_N.txt (pb_costco), or drop a
    PayPal activity CSV export in as inbox/*.csv, then run this command
    with no arguments to drain everything at once. Successfully parsed
    files are archived (Target -> data/target-orders/pasted/, Costco ->
    data/costco-orders/pasted/, PayPal -> data/paypal/); failures and
    unrecognized filenames are left in inbox/ so you can inspect and retry.
    """
    config = load_config()
    inbox_dir = resolve_path("inbox")
    inbox_dir.mkdir(parents=True, exist_ok=True)

    target_orders_dir = resolve_path("data/target-orders")
    costco_orders_dir = resolve_path(config.get("costco_orders_dir", "data/costco-orders"))
    paypal_records_path = resolve_path(config.get("paypal_records_path", "data/paypal/records.json"))

    report = import_inbox(
        inbox_dir=inbox_dir,
        target_orders_dir=target_orders_dir,
        target_archive_dir=target_orders_dir / "pasted",
        costco_orders_dir=costco_orders_dir,
        costco_archive_dir=costco_orders_dir / "pasted",
        paypal_records_path=paypal_records_path,
        paypal_archive_dir=paypal_records_path.parent,
        files=list(files) if files else None,
        keep=keep,
    )

    for line in report.lines:
        click.echo(line)
    for failure in report.failed:
        click.echo(f"{failure.source.name}: FAILED — {failure.reason}", err=True)

    total_imported = report.target_imported + report.costco_imported + report.paypal_imported
    click.echo(
        f"Imported {total_imported} file(s) "
        f"({report.target_imported} Target, {report.costco_imported} Costco, "
        f"{report.paypal_imported} PayPal / {report.paypal_new_records} new records), "
        f"skipped {report.target_skipped} Target (gift card/coupon only), "
        f"failed {len(report.failed)}"
    )
    if report.failed:
        raise SystemExit(1)


@main.command("build-paypal-review")
@click.option("--since", "since_str", default=None, help="Only include transactions on or after YYYY-MM-DD")
def build_paypal_review_cmd(since_str: str | None) -> None:
    """Build the PayPal review tab: unapproved Paypal-account transactions, enriched with PayPal notes."""
    since_override = date.fromisoformat(since_str) if since_str else None
    result = build_paypal_review(since_override)
    linked = sum(1 for item in result["items"] if item.get("paypal"))
    prefilled = sum(1 for item in result["items"] if item.get("category_name"))
    click.echo(
        f"PayPal review built: {len(result['items'])} unapproved transactions on {result['account_name']}, "
        f"{linked} linked to a PayPal note, {prefilled} pre-filled from config/paypal.yaml"
    )
    config = load_config()
    click.echo(f"Review written to {resolve_path(config.get('paypal_review_path', 'data/paypal/review.json'))}")


@main.command("propose-paypal")
def propose_paypal_cmd() -> None:
    """Re-run config/paypal.yaml rules against the existing PayPal review, filling in blanks.

    Use this after adding/editing a rule in config/paypal.yaml, instead of
    build-paypal-review, when you don't want to re-fetch from YNAB or
    re-link CSV records — just apply the new rule to pending items that
    don't have a category yet.
    """
    config = load_config()
    review_path = resolve_path(config.get("paypal_review_path", "data/paypal/review.json"))
    updated = reapply_paypal_rules(review_path)
    click.echo(f"Updated {updated} pending item(s) from config/paypal.yaml rules")


@main.command("propose")
@click.option("--since", "since_str", default=None, help="Only propose orders on or after YYYY-MM-DD")
@click.option("--until", "until_str", default=None, help="Only propose orders on or before YYYY-MM-DD")
def propose_cmd(since_str: str | None, until_str: str | None) -> None:
    """Match saved Target orders to YNAB and write review proposals."""
    since_override = date.fromisoformat(since_str) if since_str else None
    until_override = date.fromisoformat(until_str) if until_str else None
    result = run_propose(since_override, until_override)
    until_note = f" through {until_override}" if until_override else ""
    click.echo(
        f"Proposed since {result.since_date}{until_note}: {len(result.proposals)} matched, "
        f"{len(result.unmatched_orders)} unmatched orders, "
        f"{len(result.unmatched_transactions)} unmatched txns"
    )
    config = load_config()
    click.echo(f"Proposals written to {resolve_path(config['proposals_path'])}")


@main.command("propose-costco")
@click.option("--since", "since_str", default=None, help="Only propose receipts on or after YYYY-MM-DD")
@click.option("--until", "until_str", default=None, help="Only propose receipts on or before YYYY-MM-DD")
def propose_costco_cmd(since_str: str | None, until_str: str | None) -> None:
    """Match saved Costco receipts to YNAB and write review proposals."""
    since_override = date.fromisoformat(since_str) if since_str else None
    until_override = date.fromisoformat(until_str) if until_str else None
    result = run_costco_propose(since_override, until_override)
    until_note = f" through {until_override}" if until_override else ""
    click.echo(
        f"Proposed since {result.since_date}{until_note}: {len(result.proposals)} matched, "
        f"{len(result.unmatched_orders)} unmatched receipts, "
        f"{len(result.unmatched_transactions)} unmatched txns"
    )
    config = load_config()
    click.echo(
        f"Proposals written to {resolve_path(config.get('costco_proposals_path', 'data/proposals/costco-latest.json'))}"
    )


@main.command("review")
@click.option("--port", default=8765, show_default=True)
def review_cmd(port: int) -> None:
    """Start local web UI to review and approve splits."""
    uvicorn.run(
        "ynab_helper.web.app:app",
        host="127.0.0.1",
        port=port,
        reload=False,
    )


@main.command("undo")
@click.option("--last", "count", default=1, show_default=True)
def undo_cmd(count: int) -> None:
    """Revert the last N approved splits."""
    restored = undo_last(count)
    if not restored:
        click.echo("Nothing to undo")
    else:
        click.echo(f"Restored: {', '.join(restored)}")


@main.command("target-login")
def target_login_cmd() -> None:
    """Open Target login and save session to auth/target.json."""
    config = load_config()
    auth_path = resolve_path(config["target_auth_path"])
    save_target_session(auth_path)


if __name__ == "__main__":
    main()
