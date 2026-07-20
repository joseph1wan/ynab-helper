# ADR 005: YNAB Bootstrap Date

## Status

Accepted

## Context

First run needs a Target scrape start date. Manual configuration is error-prone.

## Decision

On first run (no `data/state.json`), query YNAB for uncategorized TARGET transactions and use the oldest transaction date as the scrape start. Allow override via `--since YYYY-MM-DD` or `initial_since` in config.

## Consequences

- Zero-config first run when uncategorized txns exist
- Exits with a clear message if no uncategorized TARGET txns and no override
- Subsequent runs use `last_successful_run` from state
