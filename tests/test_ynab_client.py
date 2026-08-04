from __future__ import annotations

import httpx
import pytest

from ynab_helper.ynab_client import YnabApiError, YnabClient


def _client_with_transport(transport: httpx.MockTransport) -> YnabClient:
    client = YnabClient("fake-token", "budget-1")
    client._client = httpx.Client(
        base_url="https://api.ynab.com/v1",
        headers={"Authorization": "Bearer fake-token"},
        transport=transport,
    )
    return client


def test_get_all_unapproved_transactions_excludes_transfers_and_ready_to_assign() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/categories"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "category_groups": [
                            {
                                "categories": [
                                    {"id": "cat-rta", "name": "Inflow: Ready to Assign"},
                                    {"id": "cat-groceries", "name": "Groceries"},
                                ]
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "t-approved",
                            "date": "2026-01-01",
                            "amount": -1000,
                            "payee_name": "A",
                            "category_id": None,
                            "account_id": "acc-1",
                            "approved": True,
                        },
                        {
                            "id": "t-transfer",
                            "date": "2026-01-01",
                            "amount": -1000,
                            "payee_name": "B",
                            "category_id": None,
                            "account_id": "acc-1",
                            "approved": False,
                            "transfer_account_id": "acc-2",
                        },
                        {
                            "id": "t-ready-to-assign",
                            "date": "2026-01-01",
                            "amount": 1000,
                            "payee_name": None,
                            "category_id": "cat-rta",
                            "account_id": "acc-1",
                            "approved": False,
                        },
                        {
                            "id": "t-unclaimed",
                            "date": "2026-01-01",
                            "amount": -2000,
                            "payee_name": "C",
                            "category_id": None,
                            "account_id": "acc-1",
                            "approved": False,
                        },
                    ]
                }
            },
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    results = client.get_all_unapproved_transactions()
    assert [txn.id for txn in results] == ["t-unclaimed"]


def test_patch_transactions_bulk_surfaces_ynab_error_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"id": "400", "name": "bad_request", "detail": "Category is not valid"}},
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(YnabApiError, match="Category is not valid"):
        client.patch_transactions_bulk([{"id": "t1", "category_id": "bad"}])
