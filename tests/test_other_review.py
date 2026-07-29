from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ynab_helper import other_review
from ynab_helper.models import YnabTransaction
from ynab_helper.source_scope import SourceScope


def _txn(txn_id: str, account_id: str = "acc-checking", payee_name: str | None = "UNKNOWN PAYEE") -> YnabTransaction:
    return YnabTransaction(
        id=txn_id,
        date=date(2026, 1, 1),
        amount=-5000,
        payee_name=payee_name,
        category_id=None,
        memo=None,
        account_id=account_id,
        cleared="uncleared",
        approved=False,
    )


class _FakeYnabClient:
    def __init__(self, transactions: list[YnabTransaction]) -> None:
        self._transactions = transactions
        self.patched: list[dict[str, Any]] = []
        self.bulk_patched: list[dict[str, Any]] | None = None

    def __enter__(self) -> "_FakeYnabClient":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def get_all_unapproved_transactions(self, since_date: date | None = None) -> list[YnabTransaction]:
        return self._transactions

    def patch_transaction_fields(
        self, transaction_id: str, category_id: str | None, memo: str | None, approved: bool = True
    ) -> dict[str, Any]:
        self.patched.append({"id": transaction_id, "category_id": category_id, "approved": approved})
        return {"id": transaction_id}

    def patch_transactions_bulk(self, transactions: list[dict[str, Any]]) -> dict[str, Any]:
        self.bulk_patched = transactions
        return {"transactions": transactions}


@pytest.fixture
def config(tmp_path: Path) -> dict[str, Any]:
    return {
        "ynab_token": "fake-token",
        "budget_id": "last-used",
        "other_review_path": str(tmp_path / "review.json"),
    }


def test_build_other_review_excludes_claimed_transactions(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]
) -> None:
    transactions = [
        _txn("target-1", payee_name="TARGET STORE"),
        _txn("paypal-1", account_id="acc-paypal"),
        _txn("unclaimed-1", payee_name="RANDOM SHOP"),
    ]
    fake_client = _FakeYnabClient(transactions)

    monkeypatch.setattr(other_review, "load_config", lambda: config)
    monkeypatch.setattr(other_review, "resolve_path", lambda p: Path(p))
    monkeypatch.setattr(other_review, "YnabClient", lambda token, budget_id: fake_client)
    monkeypatch.setattr(
        other_review,
        "all_source_scopes",
        lambda cfg, client: [
            SourceScope(payee_pattern="TARGET"),
            SourceScope(account_ids={"acc-paypal"}),
        ],
    )

    result = other_review.build_other_review()
    ids = [item["ynab_transaction"]["id"] for item in result["items"]]
    assert ids == ["unclaimed-1"]


def test_recategorize_rejects_unknown_category(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps({"items": [{"ynab_transaction": {"id": "t1"}, "category_name": None, "category_id": None, "status": "pending"}]})
    )
    monkeypatch.setattr(other_review, "load_categories", lambda: {"Groceries": "cat-groceries"})

    with pytest.raises(ValueError):
        other_review.recategorize_other_item(review_path, 0, "Not A Real Category")


def test_recategorize_accepts_any_real_category(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps({"items": [{"ynab_transaction": {"id": "t1"}, "category_name": None, "category_id": None, "status": "pending"}]})
    )
    monkeypatch.setattr(other_review, "load_categories", lambda: {"Gas & Parking": "cat-gas"})

    item = other_review.recategorize_other_item(review_path, 0, "Gas & Parking")
    assert item["category_id"] == "cat-gas"


def test_apply_other_item_marks_applied_and_saves_undo(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]
) -> None:
    review_path = Path(config["other_review_path"])
    review_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "ynab_transaction": {"id": "t1", "amount": -5000, "payee_name": "X", "memo": None, "category_id": None},
                        "category_name": "Groceries",
                        "category_id": "cat-groceries",
                        "status": "pending",
                    }
                ]
            }
        )
    )
    fake_client = _FakeYnabClient([])
    saved_snapshots: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(other_review, "load_config", lambda: config)
    monkeypatch.setattr(other_review, "resolve_path", lambda p: Path(p))
    monkeypatch.setattr(other_review, "YnabClient", lambda token, budget_id: fake_client)
    monkeypatch.setattr(
        other_review, "save_undo_snapshot", lambda txn_id, original: saved_snapshots.append((txn_id, original))
    )

    other_review.apply_other_item(0)

    data = json.loads(review_path.read_text())
    assert data["items"][0]["status"] == "applied"
    assert len(saved_snapshots) == 1
    assert fake_client.patched[0]["category_id"] == "cat-groceries"


def test_apply_other_item_requires_category(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]
) -> None:
    review_path = Path(config["other_review_path"])
    review_path.write_text(
        json.dumps(
            {"items": [{"ynab_transaction": {"id": "t1"}, "category_name": None, "category_id": None, "status": "pending"}]}
        )
    )
    monkeypatch.setattr(other_review, "load_config", lambda: config)
    monkeypatch.setattr(other_review, "resolve_path", lambda p: Path(p))

    with pytest.raises(ValueError):
        other_review.apply_other_item(0)


def test_clear_applied_other_items_removes_only_applied(tmp_path: Path) -> None:
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(
            {
                "items": [
                    {"ynab_transaction": {"id": "t1"}, "status": "applied"},
                    {"ynab_transaction": {"id": "t2"}, "status": "pending"},
                ]
            }
        )
    )
    removed = other_review.clear_applied_other_items(review_path)
    assert removed == 1
    data = json.loads(review_path.read_text())
    assert [item["ynab_transaction"]["id"] for item in data["items"]] == ["t2"]
