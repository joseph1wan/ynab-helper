from __future__ import annotations

from datetime import date

from ynab_helper.models import PaypalRecord, YnabTransaction
from ynab_helper.paypal_linker import link_records


def _record(name: str, amount: int, day: str, note: str = "", txn_id: str = "r1") -> PaypalRecord:
    return PaypalRecord(
        date=date.fromisoformat(day),
        name=name,
        type="General Payment",
        amount=amount,
        note=note,
        transaction_id=txn_id,
    )


def _txn(txn_id: str, amount: int, day: str, payee: str | None) -> YnabTransaction:
    return YnabTransaction(
        id=txn_id,
        date=date.fromisoformat(day),
        amount=amount,
        payee_name=payee,
        category_id=None,
        memo=None,
        account_id="acc-1",
        cleared="uncleared",
        approved=False,
    )


def test_unique_amount_match_within_window() -> None:
    records = [_record("Casey Tianshu Ching", 50_000, "2026-03-18", "thank you", "r1")]
    txns = [_txn("t1", 50_000, "2026-03-19", "Casey Tianshu Ching")]

    result = link_records(records, txns)

    record, via, candidates = result["t1"]
    assert record is not None and record.transaction_id == "r1"
    assert via == "amount"
    assert candidates == [record]


def test_outside_window_is_unlinked() -> None:
    records = [_record("Casey Tianshu Ching", 50_000, "2026-03-18", "thank you", "r1")]
    txns = [_txn("t1", 50_000, "2026-03-25", "Casey Tianshu Ching")]

    result = link_records(records, txns)

    record, via, candidates = result["t1"]
    assert record is None
    assert via is None
    assert candidates == []


def test_ambiguous_amount_resolved_by_payee_name() -> None:
    records = [
        _record("Marvin Tsang", 5_000, "2026-04-05", "coffee", "r1"),
        _record("Po-Ying Liu", 5_000, "2026-04-05", "lunch", "r2"),
    ]
    txns = [_txn("t1", 5_000, "2026-04-05", "Marvin Tsang")]

    result = link_records(records, txns)

    record, via, candidates = result["t1"]
    assert record is not None and record.transaction_id == "r1"
    assert via == "name"
    assert len(candidates) == 2


def test_still_ambiguous_when_name_does_not_disambiguate() -> None:
    records = [
        _record("Marvin Tsang", 5_000, "2026-04-05", "coffee", "r1"),
        _record("Marvin Tsang", 5_000, "2026-04-06", "lunch", "r2"),
    ]
    txns = [_txn("t1", 5_000, "2026-04-05", "Marvin Tsang")]

    result = link_records(records, txns)

    record, via, candidates = result["t1"]
    assert record is None
    assert via is None
    assert len(candidates) == 2


def test_no_records_at_all_yields_unlinked() -> None:
    txns = [_txn("t1", 5_000, "2026-04-05", "Someone")]
    result = link_records([], txns)

    record, via, candidates = result["t1"]
    assert record is None and via is None and candidates == []
