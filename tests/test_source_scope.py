from __future__ import annotations

from datetime import date

from ynab_helper.costco_fetch import get_source_scope as costco_scope
from ynab_helper.fetch import get_source_scope as target_scope
from ynab_helper.models import YnabTransaction
from ynab_helper.paypal_review import get_source_scope as paypal_scope
from ynab_helper.source_scope import SourceScope


def _txn(account_id: str = "acc-1", payee_name: str | None = "TARGET STORE") -> YnabTransaction:
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


def test_claims_account_only_scope() -> None:
    scope = SourceScope(account_ids={"acc-1"})
    assert scope.claims(_txn(account_id="acc-1"))
    assert not scope.claims(_txn(account_id="acc-2"))


def test_claims_payee_only_scope() -> None:
    scope = SourceScope(payee_pattern="TARGET")
    assert scope.claims(_txn(payee_name="TARGET STORE #1234"))
    assert not scope.claims(_txn(payee_name="COSTCO WHOLESALE"))
    assert not scope.claims(_txn(payee_name=None))


def test_claims_combined_scope() -> None:
    scope = SourceScope(account_ids={"acc-1"}, payee_pattern="COSTCO")
    assert scope.claims(_txn(account_id="acc-1", payee_name="COSTCO GAS"))
    assert not scope.claims(_txn(account_id="acc-2", payee_name="COSTCO GAS"))
    assert not scope.claims(_txn(account_id="acc-1", payee_name="TARGET"))


def test_unrestricted_scope_always_claims() -> None:
    scope = SourceScope()
    assert scope.claims(_txn())


class _FakeClient:
    def __init__(self, account_ids: dict[str, str]) -> None:
        self._account_ids = account_ids

    def get_account_id_by_name(self, name: str) -> str | None:
        return self._account_ids.get(name)

    def list_accounts(self) -> dict[str, str]:
        return self._account_ids


def test_target_scope_reads_payee_pattern() -> None:
    scope = target_scope({"payee_pattern": "TARGET"}, _FakeClient({}))
    assert scope == SourceScope(payee_pattern="TARGET")


def test_target_scope_defaults_to_target() -> None:
    scope = target_scope({}, _FakeClient({}))
    assert scope.payee_pattern == "TARGET"


def test_paypal_scope_reads_account_name() -> None:
    client = _FakeClient({"Paypal": "acc-paypal"})
    scope = paypal_scope({"paypal_account_name": "Paypal"}, client)
    assert scope == SourceScope(account_ids={"acc-paypal"})


def test_paypal_scope_missing_account_claims_nothing() -> None:
    client = _FakeClient({})
    scope = paypal_scope({"paypal_account_name": "Paypal"}, client)
    assert scope.account_ids == set()
    assert not scope.claims(_txn(account_id="acc-1"))


def test_costco_scope_reads_accounts_and_pattern() -> None:
    client = _FakeClient({"Sapphire": "acc-1", "Bilt": "acc-2"})
    scope = costco_scope(
        {"costco_account_names": ["Sapphire", "Bilt"], "costco_payee_pattern": "COSTCO"},
        client,
    )
    assert scope == SourceScope(account_ids={"acc-1", "acc-2"}, payee_pattern="COSTCO")


def test_costco_scope_ignores_unknown_account_names() -> None:
    client = _FakeClient({"Sapphire": "acc-1"})
    scope = costco_scope({"costco_account_names": ["Sapphire", "Unknown"]}, client)
    assert scope.account_ids == {"acc-1"}
