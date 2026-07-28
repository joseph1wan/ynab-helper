"""Drain manually exported PayPal activity CSVs into cached PaypalRecord JSON.

Workflow: export PayPal activity as CSV (Activity → Statements → ... → CSV),
save it into data/paypal/inbox/*.CSV, then run `ynab-helper import-paypal`.
Each file is parsed with parse_paypal_csv, merged into data/paypal/records.json
(deduped on Transaction ID so overlapping exports are safe to re-import), and
archived to data/paypal/ so it can be re-parsed later without re-exporting.
Files that fail to parse are left in the inbox untouched.

"Bank Deposit to PP Account" rows are BoA -> Paypal transfers (Paypal is an
on-budget checking account in YNAB) and carry no counterparty or note, so
they are dropped at parse time — they need no category and enrich nothing.

The CSV is UTF-8 with a BOM, amounts use comma thousands separators, and
Type values are whitespace-padded — all handled here.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ynab_helper.models import PaypalRecord

_EXCLUDED_TYPES = {"Bank Deposit to PP Account"}


@dataclass
class ImportedCsv:
    source: Path
    record_count: int


@dataclass
class ImportFailure:
    source: Path
    reason: str


@dataclass
class ImportReport:
    imported: list[ImportedCsv] = field(default_factory=list)
    failed: list[ImportFailure] = field(default_factory=list)
    new_records: int = 0


def _amount_to_milliunits(value: str) -> int:
    return int(round(float(value.replace(",", "")) * 1000))


def _parse_date(value: str) -> date:
    month, day, year = value.split("/")
    return date(int(year), int(month), int(day))


def _note(row: dict[str, str]) -> str:
    # PayPal has exported this column as both "Item Title" and "Memo" across
    # different downloads of the same activity; accept either.
    return (row.get("Item Title") or row.get("Memo") or "").strip()


def parse_paypal_csv(path: Path) -> list[PaypalRecord]:
    records: list[PaypalRecord] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row_type = (row.get("Type") or "").strip()
            if row_type in _EXCLUDED_TYPES:
                continue
            records.append(
                PaypalRecord(
                    date=_parse_date(row["Date"]),
                    name=(row.get("Name") or "").strip(),
                    type=row_type,
                    amount=_amount_to_milliunits(row["Amount"]),
                    note=_note(row),
                    transaction_id=row["Transaction ID"],
                )
            )
    return records


def _serialize(record: PaypalRecord) -> dict[str, object]:
    return {
        "date": record.date.isoformat(),
        "name": record.name,
        "type": record.type,
        "amount": record.amount,
        "note": record.note,
        "transaction_id": record.transaction_id,
    }


def _deserialize(raw: dict[str, object]) -> PaypalRecord:
    return PaypalRecord(
        date=date.fromisoformat(raw["date"]),
        name=raw["name"],
        type=raw["type"],
        amount=raw["amount"],
        note=raw["note"],
        transaction_id=raw["transaction_id"],
    )


def load_paypal_records(records_path: Path) -> list[PaypalRecord]:
    if not records_path.exists():
        return []
    with records_path.open() as f:
        raw = json.load(f)
    return [_deserialize(r) for r in raw]


def save_paypal_records(records_path: Path, records: list[PaypalRecord]) -> None:
    records_path.parent.mkdir(parents=True, exist_ok=True)
    with records_path.open("w") as f:
        json.dump([_serialize(r) for r in records], f, indent=2)


def import_paypal_csvs(
    inbox_dir: Path,
    archive_dir: Path,
    records_path: Path,
    files: list[Path] | None = None,
    keep: bool = False,
) -> ImportReport:
    """Parse PayPal activity CSVs and merge them into records_path.

    If `files` is given, only those files are processed (still archived to
    archive_dir unless `keep`). Otherwise every *.CSV in inbox_dir is
    processed. Records are deduped on Transaction ID across the whole merge.
    """
    report = ImportReport()
    targets = files if files is not None else sorted(inbox_dir.glob("*.CSV"))

    existing = load_paypal_records(records_path)
    by_id = {r.transaction_id: r for r in existing}

    if not keep:
        archive_dir.mkdir(parents=True, exist_ok=True)

    for path in targets:
        try:
            parsed = parse_paypal_csv(path)
        except (OSError, KeyError, ValueError, csv.Error) as exc:
            report.failed.append(ImportFailure(source=path, reason=str(exc)))
            continue

        for record in parsed:
            if record.transaction_id not in by_id:
                by_id[record.transaction_id] = record
                report.new_records += 1

        if not keep:
            path.replace(archive_dir / path.name)

        report.imported.append(ImportedCsv(source=path, record_count=len(parsed)))

    save_paypal_records(records_path, list(by_id.values()))
    return report
