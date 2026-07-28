from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import click
import uvicorn

from ynab_helper.config import CONFIG_DIR, load_categories, load_config, load_rules, resolve_path
from ynab_helper.fetch import run_fetch, run_propose
from ynab_helper.invoice_import import import_pasted_invoices
from ynab_helper.rules_audit import build_report, render_text, report_to_dict
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
    help="Parse without moving the source .txt out of the inbox (or wherever FILES live)",
)
def import_invoices_cmd(files: tuple[Path, ...], keep: bool) -> None:
    """Parse manually pasted Target invoice text into cached order JSON.

    Workflow: open an invoice on target.com, select-all + copy the page,
    save it as a .txt file in data/target-orders/pasted/inbox/, then run
    this command with no arguments to drain the inbox. Successfully parsed
    files are archived to data/target-orders/pasted/; failures are left in
    place so you can inspect and retry.
    """
    orders_dir = resolve_path("data/target-orders")
    inbox_dir = orders_dir / "pasted" / "inbox"
    archive_dir = orders_dir / "pasted"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    report = import_pasted_invoices(
        inbox_dir=inbox_dir,
        archive_dir=archive_dir,
        output_dir=orders_dir,
        files=list(files) if files else None,
        keep=keep,
    )

    for item in report.imported:
        click.echo(
            f"{item.source.name} -> {item.output_path.name} "
            f"({item.item_count} item{'s' if item.item_count != 1 else ''}, "
            f"${item.total / 1000:.2f})"
        )
    for failure in report.failed:
        click.echo(f"{failure.source.name}: FAILED — {failure.reason}", err=True)

    click.echo(f"Imported {len(report.imported)}, failed {len(report.failed)}")
    if report.failed:
        raise SystemExit(1)


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


@main.command("audit-rules")
@click.option("--since", "since_str", default=None, help="Only audit orders on or after YYYY-MM-DD")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.option("--strict", is_flag=True, help="Exit non-zero on warnings too, not just errors")
@click.option("--try-pattern", "try_pattern", default=None, help="Splice a hypothetical rule pattern")
@click.option("--try-category", "try_category", default=None, help="Category for --try-pattern")
@click.option(
    "--try-at",
    "try_at",
    default=None,
    type=int,
    help="Index to insert the hypothetical rule at (default: end)",
)
def audit_rules_cmd(
    since_str: str | None,
    as_json: bool,
    strict: bool,
    try_pattern: str | None,
    try_category: str | None,
    try_at: int | None,
) -> None:
    """Audit config/rules.yaml against cached Target orders: unmatched items,
    wrong-rule collisions, and static rule validation."""
    if (try_pattern is None) != (try_category is None):
        raise click.ClickException("--try-pattern and --try-category must be used together")

    since_date = date.fromisoformat(since_str) if since_str else date.min
    orders = load_cached_orders(resolve_path("data/target-orders"), since_date)

    rules_data = load_rules()
    rules = list(rules_data.get("rules", []))
    fallback_category = rules_data.get("fallback_category", "Shopping")
    allowed_categories = list(rules_data.get("allowed_categories", []))

    if try_pattern is not None and try_category is not None:
        insert_at = len(rules) if try_at is None else try_at
        rules.insert(insert_at, {"pattern": try_pattern, "category": try_category})

    categories = load_categories()

    report = build_report(orders, rules, fallback_category, categories, allowed_categories)

    if as_json:
        click.echo(json.dumps(report_to_dict(report), indent=2))
    else:
        click.echo(render_text(report))

    has_errors = any(issue.severity == "error" for issue in report.issues)
    has_warnings = any(issue.severity == "warning" for issue in report.issues)
    if has_errors or (strict and has_warnings):
        raise SystemExit(1)


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
