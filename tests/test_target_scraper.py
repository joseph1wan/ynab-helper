from __future__ import annotations

from pathlib import Path

from ynab_helper.target_scraper import _build_browser_launch_kwargs


def test_build_browser_launch_kwargs_uses_default_chrome_profile(tmp_path: Path) -> None:
    profile_root = tmp_path / "google-chrome"

    kwargs = _build_browser_launch_kwargs(headless=False, profile_root=profile_root)

    assert kwargs["channel"] == "chrome"
    assert kwargs["args"] == [
        f"--user-data-dir={profile_root}",
        "--profile-directory=Default",
    ]
