from datetime import date
from pathlib import Path

from ynab_helper.state import resolve_since_date, save_state


def test_normal_fetch_uses_newer_of_configured_and_last_successful_run(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        {
            "bootstrap_since": "2026-07-01",
            "last_successful_run": "2026-07-20T15:00:00+00:00",
        },
    )

    since_date, is_first_run = resolve_since_date(
        state_path, None, date(2026, 7, 10), None
    )

    assert since_date == date(2026, 7, 20)
    assert not is_first_run


def test_overwrite_ignores_last_successful_run(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        {
            "bootstrap_since": "2026-07-01",
            "last_successful_run": "2026-07-20T15:00:00+00:00",
        },
    )

    since_date, is_first_run = resolve_since_date(
        state_path, None, date(2026, 7, 10), None, overwrite=True
    )

    assert since_date == date(2026, 7, 10)
    assert not is_first_run
