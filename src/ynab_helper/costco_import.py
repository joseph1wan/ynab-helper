"""Drain manually pasted Costco receipt .txt files into cached order JSON.

Workflow: copy a Costco receipt's rendered text (Gas Station or In-Warehouse)
into data/costco-orders/pasted/inbox/*.txt, then run
`ynab-helper import-costco-receipts`. Each file is parsed by
costco_receipt_text.parse_receipt_text, written as
data/costco-orders/{receipt_id}.json, and archived to
data/costco-orders/pasted/receipt_{receipt_id}.txt so it can be re-parsed
later without re-copying from Costco. Files that fail to parse are left in
the inbox untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ynab_helper.costco_receipt_text import ParsedReceipt, parse_receipt_text


@dataclass
class ImportedReceipt:
    source: Path
    receipt_id: str
    output_path: Path
    item_count: int
    total: int


@dataclass
class ImportFailure:
    source: Path
    reason: str


@dataclass
class ImportReport:
    imported: list[ImportedReceipt] = field(default_factory=list)
    failed: list[ImportFailure] = field(default_factory=list)


def _order_json_dict(parsed: ParsedReceipt) -> dict:
    return {
        "receipt_id": parsed.receipt_id,
        "receipt_date": parsed.receipt_date.isoformat(),
        "total": parsed.total,
        "tax": parsed.tax,
        "shipping": 0,
        "fees": 0,
        "receipt_type": parsed.receipt_type,
        "store_number": parsed.store_number,
        "transaction_number": parsed.transaction_number,
        "line_items": [
            {"name": li.name, "quantity": li.quantity, "line_total": li.line_total}
            for li in parsed.line_items
        ],
    }


def import_pasted_receipts(
    inbox_dir: Path,
    archive_dir: Path,
    output_dir: Path,
    files: list[Path] | None = None,
    keep: bool = False,
) -> ImportReport:
    """Parse pasted Costco receipt .txt files and write them into output_dir.

    If `files` is given, only those files are processed (still archived to
    archive_dir / moved out of wherever they were, unless `keep`). Otherwise
    every *.txt in inbox_dir is processed.
    """
    report = ImportReport()
    targets = files if files is not None else sorted(inbox_dir.glob("*.txt"))

    output_dir.mkdir(parents=True, exist_ok=True)
    if not keep:
        archive_dir.mkdir(parents=True, exist_ok=True)

    for path in targets:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            report.failed.append(ImportFailure(source=path, reason=f"could not read file: {exc}"))
            continue

        parsed = parse_receipt_text(text)
        if parsed is None:
            report.failed.append(
                ImportFailure(
                    source=path,
                    reason="could not detect receipt type, or find store/transaction id, date, total, or items",
                )
            )
            continue

        out_path = output_dir / f"{parsed.receipt_id}.json"
        payload = _order_json_dict(parsed)
        with out_path.open("w") as f:
            json.dump(payload, f, indent=2)

        if not keep:
            archive_path = archive_dir / f"receipt_{parsed.receipt_id}.txt"
            path.replace(archive_path)

        report.imported.append(
            ImportedReceipt(
                source=path,
                receipt_id=parsed.receipt_id,
                output_path=out_path,
                item_count=len(parsed.line_items),
                total=parsed.total,
            )
        )

    return report
