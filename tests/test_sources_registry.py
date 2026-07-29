from __future__ import annotations

from datetime import date

from ynab_helper.models import YnabTransaction
from ynab_helper.source_scope import SourceScope
from ynab_helper.sources import all_source_scopes, is_claimed


def _txn(account_id: str = "acc-1", payee_name: str | None = "SOME PAYEE") -> YnabTransaction:
    return YnabTransaction(
        id="t1",
        date=date(2026, 1, 1),
        amount=-1000,
        payee_name=payee_name,
        category_id=None,
        memo=None,
        account_id=account_id,
        cleared="uncleared",
        approved=False,
    )


def test_all_source_scopes_returns_four_in_fixed_order() -> None:
    class _FakeClient:
        def get_account_id_by_name(self, name: str) -> str | None:
            return "acc-paypal"

        def list_accounts(self) -> dict[str, str]:
            return {"Sapphire": "acc-costco"}

    scopes = all_source_scopes(
        {
            "payee_pattern": "TARGET",
            "costco_account_names": ["Sapphire"],
            "costco_payee_pattern": "COSTCO",
            "paypal_account_name": "Paypal",
            "amazon_payee_pattern": "AMAZON",
        },
        _FakeClient(),
    )
    assert len(scopes) == 4
    assert scopes[0] == SourceScope(payee_pattern="TARGET")
    assert scopes[2] == SourceScope(account_ids={"acc-paypal"})
    assert scopes[3] == SourceScope(payee_pattern="AMAZON")


def test_is_claimed_true_when_any_scope_claims() -> None:
    scopes = [SourceScope(payee_pattern="TARGET"), SourceScope(account_ids={"acc-1"})]
    assert is_claimed(_txn(account_id="acc-1", payee_name="COSTCO"), scopes)


def test_is_claimed_false_when_no_scope_claims() -> None:
    scopes = [SourceScope(payee_pattern="TARGET"), SourceScope(account_ids={"acc-1"})]
    assert not is_claimed(_txn(account_id="acc-2", payee_name="COSTCO"), scopes)
