from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import click
import uvicorn

from ynab_helper.config import CONFIG_DIR, load_config, resolve_path
from ynab_helper.fetch import run_fetch, run_propose
from ynab_helper.target_scraper import save_target_session
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
@click.option(
    "--overwrite",
    is_flag=True,
    help="Ignore the last successful fetch and re-scrape from --since/bootstrap date",
)
@click.option("--skip-scrape", is_flag=True, help="Use cached Target orders only")
@click.option("--headed/--headless", default=True, help="Run browser with visible window")
@click.option("--debug-pause", is_flag=True, help="Pause after each scraper step until Enter is pressed")
def fetch_cmd(
    since_str: str | None,
    overwrite: bool,
    skip_scrape: bool,
    headed: bool,
    debug_pause: bool,
) -> None:
    """Scrape Target orders and save them locally."""
    since_override = date.fromisoformat(since_str) if since_str else None
    result = run_fetch(
        since_override=since_override,
        overwrite=overwrite,
        skip_scrape=skip_scrape,
        headless=not headed,
        debug_pause=debug_pause,
    )
    click.echo(
        f"Fetched since {result.since_date}: "
        f"saved {len(result.orders)} Target orders"
    )


@main.command("propose")
@click.option("--since", "since_str", default=None, help="Only propose orders on or after YYYY-MM-DD")
def propose_cmd(since_str: str | None) -> None:
    """Match saved Target orders to YNAB and write review proposals."""
    result = run_propose(date.fromisoformat(since_str) if since_str else None)
    click.echo(
        f"Proposed since {result.since_date}: {len(result.proposals)} matched, "
        f"{len(result.unmatched_orders)} unmatched orders, "
        f"{len(result.unmatched_transactions)} unmatched txns"
    )
    config = load_config()
    click.echo(f"Proposals written to {resolve_path(config['proposals_path'])}")


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
