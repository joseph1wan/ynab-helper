from __future__ import annotations

from pathlib import Path

from ynab_helper.paypal_csv import (
    import_paypal_csvs,
    load_paypal_records,
    parse_paypal_csv,
    save_paypal_records,
)

FIXTURE = Path(__file__).parent / "fixtures" / "paypal_activity.csv"


def test_parse_paypal_csv_excludes_deposits_and_reads_bom_and_commas() -> None:
    records = parse_paypal_csv(FIXTURE)

    assert len(records) == 92
    assert all(r.type != "Bank Deposit to PP Account" for r in records)

    with_notes = [r for r in records if r.note]
    assert len(with_notes) == 88
    assert all(not r.note.endswith("...") for r in with_notes)


def test_parse_paypal_csv_amounts_and_signed_values() -> None:
    records = parse_paypal_csv(FIXTURE)
    casey = next(r for r in records if r.name == "Casey Tianshu Ching")
    assert casey.amount == 50_000
    assert casey.date.isoformat() == "2026-03-18"

    soua = next(r for r in records if r.name == "Soua Vang")
    assert soua.amount == -150_000


def test_import_paypal_csvs_archives_and_dedupes(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    archive = tmp_path / "archive"
    records_path = tmp_path / "records.json"

    (inbox / "export.CSV").write_bytes(FIXTURE.read_bytes())

    report = import_paypal_csvs(inbox, archive, records_path, files=None, keep=False)
    assert len(report.imported) == 1
    assert report.new_records == 92
    assert not report.failed
    assert not (inbox / "export.CSV").exists()
    assert (archive / "export.CSV").exists()

    records = load_paypal_records(records_path)
    assert len(records) == 92

    # Re-importing the same (now archived) file again should dedupe to zero new records.
    report2 = import_paypal_csvs(inbox, archive, records_path, files=[archive / "export.CSV"], keep=True)
    assert report2.new_records == 0
    assert len(load_paypal_records(records_path)) == 92


def test_import_paypal_csvs_reports_failures(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    archive = tmp_path / "archive"
    records_path = tmp_path / "records.json"

    bad = inbox / "broken.CSV"
    bad.write_text("not,a,valid,paypal,csv\n1,2,3,4,5\n")

    report = import_paypal_csvs(inbox, archive, records_path)
    assert len(report.failed) == 1
    assert report.failed[0].source == bad
    assert bad.exists()  # left in place on failure


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    records_path = tmp_path / "records.json"
    records = parse_paypal_csv(FIXTURE)[:3]
    save_paypal_records(records_path, records)
    loaded = load_paypal_records(records_path)
    assert loaded == records
