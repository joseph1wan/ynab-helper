from __future__ import annotations

import json
from pathlib import Path

import pytest

from ynab_helper.fetch import clear_applied, recategorize_line, set_line_note


def _proposal(status: str = "pending") -> dict:
    return {
        "target_order": {
            "order_id": "o1",
            "order_date": "2026-07-01",
            "total": 10000,
            "tax": 0,
            "shipping": 0,
            "fees": 0,
            "line_items": [
                {"name": "Diapers", "quantity": 1, "line_total": 6000},
                {"name": "Milk", "quantity": 1, "line_total": 4000},
            ],
        },
        "ynab_transaction": {
            "id": "txn-1",
            "date": "2026-07-01",
            "amount": -10000,
            "payee_name": "TARGET",
            "category_id": None,
            "memo": None,
            "account_id": "acct-1",
        },
        "categorized_lines": [
            {
                "name": "Diapers",
                "quantity": 1,
                "line_total": 6000,
                "category_name": "Shopping",
                "category_id": "cat-shopping",
                "matched_rule": None,
                "note": None,
            },
            {
                "name": "Milk",
                "quantity": 1,
                "line_total": 4000,
                "category_name": "Groceries",
                "category_id": "cat-groceries",
                "matched_rule": "milk",
                "note": None,
            },
        ],
        "splits": [],
        "unmatched_items": [{"name": "Diapers", "quantity": 1, "line_total": 6000}],
        "rounding_delta": 0,
        "status": status,
    }


@pytest.fixture
def proposals_file(tmp_path: Path) -> Path:
    path = tmp_path / "latest.json"
    data = {
        "fetched_at": "2026-07-01T00:00:00+00:00",
        "since_date": "2026-07-01",
        "proposals": [_proposal()],
        "unmatched_orders": [],
        "unmatched_transactions": [],
    }
    path.write_text(json.dumps(data))
    return path


@pytest.fixture(autouse=True)
def fake_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ynab_helper.fetch.load_categories",
        lambda: {"Shopping": "cat-shopping", "Groceries": "cat-groceries", "Baby": "cat-baby"},
    )
    monkeypatch.setattr(
        "ynab_helper.fetch.load_rules",
        lambda: {"allowed_categories": ["Shopping", "Groceries", "Baby"]},
    )


def test_recategorize_line_updates_category_and_splits(proposals_file: Path) -> None:
    proposal = recategorize_line(proposals_file, 0, 0, "Baby")

    line = proposal["categorized_lines"][0]
    assert line["category_name"] == "Baby"
    assert line["category_id"] == "cat-baby"
    assert line["matched_rule"] == "manual override"
    assert sum(s["amount"] for s in proposal["splits"]) == proposal["ynab_transaction"]["amount"]

    on_disk = json.loads(proposals_file.read_text())
    assert on_disk["proposals"][0]["categorized_lines"][0]["category_name"] == "Baby"


def test_recategorize_line_clears_unmatched_item(proposals_file: Path) -> None:
    proposal = recategorize_line(proposals_file, 0, 0, "Baby")
    assert proposal["unmatched_items"] == []


def test_recategorize_line_unknown_category_raises(proposals_file: Path) -> None:
    with pytest.raises(ValueError):
        recategorize_line(proposals_file, 0, 0, "Nonexistent")


def test_recategorize_line_rejects_applied_proposal(tmp_path: Path) -> None:
    path = tmp_path / "latest.json"
    data = {"proposals": [_proposal(status="applied")]}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        recategorize_line(path, 0, 0, "Baby")


def test_set_line_note_persists_and_never_touches_splits(proposals_file: Path) -> None:
    before = json.loads(proposals_file.read_text())["proposals"][0]["splits"]
    proposal = set_line_note(proposals_file, 0, 1, "picked groceries over baby")

    assert proposal["categorized_lines"][1]["note"] == "picked groceries over baby"
    assert proposal["splits"] == before

    on_disk = json.loads(proposals_file.read_text())
    assert on_disk["proposals"][0]["categorized_lines"][1]["note"] == "picked groceries over baby"


def test_set_line_note_empty_string_clears_to_none(proposals_file: Path) -> None:
    set_line_note(proposals_file, 0, 1, "why")
    proposal = set_line_note(proposals_file, 0, 1, "")
    assert proposal["categorized_lines"][1]["note"] is None


def test_clear_applied_removes_only_applied(tmp_path: Path) -> None:
    path = tmp_path / "latest.json"
    data = {
        "proposals": [
            _proposal(status="applied"),
            _proposal(status="pending"),
        ]
    }
    path.write_text(json.dumps(data))

    removed = clear_applied(path)

    assert removed == 1
    on_disk = json.loads(path.read_text())
    assert len(on_disk["proposals"]) == 1
    assert on_disk["proposals"][0]["status"] == "pending"


def test_clear_applied_noop_when_nothing_applied(proposals_file: Path) -> None:
    assert clear_applied(proposals_file) == 0
