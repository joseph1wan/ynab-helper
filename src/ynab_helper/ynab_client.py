from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from ynab_helper.models import YnabTransaction

BASE_URL = "https://api.ynab.com/v1"


class YnabClient:
    def __init__(self, token: str, budget_id: str = "last-used") -> None:
        self.budget_id = budget_id
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> YnabClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        body = response.json()
        if body.get("data") is None:
            raise RuntimeError(f"YNAB API error: {body}")
        return body["data"]

    def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.patch(path, json={"transaction": payload})
        response.raise_for_status()
        body = response.json()
        return body["data"]["transaction"]

    def list_categories(self) -> dict[str, str]:
        """Return mapping of category name -> category id."""
        data = self._get(f"/budgets/{self.budget_id}/categories")
        categories: dict[str, str] = {}
        for group in data["category_groups"]:
            for category in group["categories"]:
                if category.get("deleted"):
                    continue
                if category["name"] == "Inflow":
                    continue
                categories[category["name"]] = category["id"]
        return categories

    def get_transactions_since(self, since_date: date) -> list[YnabTransaction]:
        data = self._get(
            f"/budgets/{self.budget_id}/transactions",
            params={"since_date": since_date.isoformat()},
        )
        return [self._parse_transaction(txn) for txn in data["transactions"]]

    def get_uncategorized_target_transactions(
        self, payee_pattern: str, since_date: date | None = None
    ) -> list[YnabTransaction]:
        params: dict[str, Any] = {}
        if since_date:
            params["since_date"] = since_date.isoformat()
        data = self._get(
            f"/budgets/{self.budget_id}/transactions",
            params=params or None,
        )
        pattern = payee_pattern.upper()
        results: list[YnabTransaction] = []
        for raw in data["transactions"]:
            txn = self._parse_transaction(raw)
            if txn.amount >= 0:
                continue
            payee = (txn.payee_name or "").upper()
            if pattern not in payee:
                continue
            if txn.subtransactions:
                continue
            if txn.category_id is not None:
                continue
            results.append(txn)
        return results

    def oldest_uncategorized_target_date(
        self, payee_pattern: str
    ) -> date | None:
        txns = self.get_uncategorized_target_transactions(payee_pattern)
        if not txns:
            return None
        return min(txn.date for txn in txns)

    def patch_transaction_splits(
        self,
        transaction_id: str,
        subtransactions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._patch(
            f"/budgets/{self.budget_id}/transactions/{transaction_id}",
            {"subtransactions": subtransactions},
        )

    def restore_transaction(
        self,
        transaction_id: str,
        original: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "amount": original["amount"],
            "payee_name": original.get("payee_name"),
            "memo": original.get("memo"),
            "category_id": original.get("category_id"),
            "subtransactions": [],
        }
        return self._patch(
            f"/budgets/{self.budget_id}/transactions/{transaction_id}",
            payload,
        )

    @staticmethod
    def _parse_transaction(raw: dict[str, Any]) -> YnabTransaction:
        return YnabTransaction(
            id=raw["id"],
            date=date.fromisoformat(raw["date"]),
            amount=raw["amount"],
            payee_name=raw.get("payee_name"),
            category_id=raw.get("category_id"),
            memo=raw.get("memo"),
            account_id=raw["account_id"],
            cleared=raw.get("cleared", "uncleared"),
            approved=raw.get("approved"),
            subtransactions=raw.get("subtransactions") or [],
        )
