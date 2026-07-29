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

    def list_accounts(self) -> dict[str, str]:
        """Return mapping of account name -> account id (excludes deleted)."""
        data = self._get(f"/budgets/{self.budget_id}/accounts")
        accounts: dict[str, str] = {}
        for account in data["accounts"]:
            if account.get("deleted"):
                continue
            accounts[account["name"]] = account["id"]
        return accounts

    def get_transactions_since(self, since_date: date) -> list[YnabTransaction]:
        data = self._get(
            f"/budgets/{self.budget_id}/transactions",
            params={"since_date": since_date.isoformat()},
        )
        return [self._parse_transaction(txn) for txn in data["transactions"]]

    def get_uncategorized_target_transactions(
        self,
        payee_pattern: str,
        since_date: date | None = None,
        until_date: date | None = None,
    ) -> list[YnabTransaction]:
        # YNAB's API only supports a since_date filter server-side; an upper
        # bound has to be applied client-side after fetching.
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
            # amount is not sign-filtered here: outflows are ordinary
            # purchases, inflows are refunds — both need to be matchable.
            if until_date is not None and txn.date > until_date:
                continue
            payee = (txn.payee_name or "").upper()
            if pattern not in payee:
                continue
            if txn.subtransactions:
                continue
            # A transaction is "done" (already handled, skip it) only once
            # it's both categorized AND approved. "approved" alone isn't
            # enough — YNAB auto-assigns a category from renaming rules on
            # import while leaving approved=false, and we still want to
            # offer our own split for those. category_id alone isn't enough
            # either — it drops manually-approved-but-never-categorized
            # transactions, which do need our proposal.
            if txn.category_id is not None and txn.approved:
                continue
            results.append(txn)
        return results

    def get_uncategorized_costco_transactions(
        self,
        account_ids: list[str],
        payee_pattern: str,
        since_date: date | None = None,
        until_date: date | None = None,
    ) -> list[YnabTransaction]:
        """Same filtering as get_uncategorized_target_transactions, plus an
        account-id membership check. Takes resolved account_ids (not names)
        so the caller resolves names -> ids once via list_accounts() and can
        raise a clear error if a configured account name isn't found,
        instead of this silently returning zero matches."""
        params: dict[str, Any] = {}
        if since_date:
            params["since_date"] = since_date.isoformat()
        data = self._get(
            f"/budgets/{self.budget_id}/transactions",
            params=params or None,
        )
        pattern = payee_pattern.upper()
        account_id_set = set(account_ids)
        results: list[YnabTransaction] = []
        for raw in data["transactions"]:
            txn = self._parse_transaction(raw)
            if until_date is not None and txn.date > until_date:
                continue
            if txn.account_id not in account_id_set:
                continue
            payee = (txn.payee_name or "").upper()
            if pattern not in payee:
                continue
            if txn.subtransactions:
                continue
            if txn.category_id is not None and txn.approved:
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

    def get_account_id_by_name(self, name: str) -> str | None:
        data = self._get(f"/budgets/{self.budget_id}/accounts")
        for account in data["accounts"]:
            if account.get("deleted") or account.get("closed"):
                continue
            if account["name"] == name:
                return account["id"]
        return None

    def get_unapproved_account_transactions(
        self, account_id: str, since_date: date | None = None
    ) -> list[YnabTransaction]:
        """Unapproved, non-transfer transactions on a single account.

        Also excludes "Inflow: Ready to Assign" rows — YNAB auto-assigns
        that category to bank-deposit inflows (e.g. a BoA -> Paypal
        transfer) that carry no counterparty or note to review; they need
        no human categorization decision.
        """
        params: dict[str, Any] = {}
        if since_date:
            params["since_date"] = since_date.isoformat()
        data = self._get(
            f"/budgets/{self.budget_id}/accounts/{account_id}/transactions",
            params=params or None,
        )
        ready_to_assign_id = self.list_categories().get("Inflow: Ready to Assign")
        results: list[YnabTransaction] = []
        for raw in data["transactions"]:
            txn = self._parse_transaction(raw)
            if txn.approved:
                continue
            if txn.transfer_account_id is not None:
                continue
            if ready_to_assign_id is not None and txn.category_id == ready_to_assign_id:
                continue
            results.append(txn)
        return results

    def patch_transaction_fields(
        self,
        transaction_id: str,
        category_id: str | None,
        memo: str | None,
        approved: bool = True,
    ) -> dict[str, Any]:
        return self._patch(
            f"/budgets/{self.budget_id}/transactions/{transaction_id}",
            {"category_id": category_id, "memo": memo, "approved": approved},
        )

    def patch_transaction_splits(
        self,
        transaction_id: str,
        subtransactions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._patch(
            f"/budgets/{self.budget_id}/transactions/{transaction_id}",
            {"subtransactions": subtransactions, "approved": True},
        )

    def patch_transactions_bulk(
        self, transactions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Bulk update transactions. Each entry must include 'id'."""
        response = self._client.patch(
            f"/budgets/{self.budget_id}/transactions",
            json={"transactions": transactions},
        )
        response.raise_for_status()
        return response.json()["data"]

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
        if "approved" in original:
            payload["approved"] = original["approved"]
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
            transfer_account_id=raw.get("transfer_account_id"),
        )
