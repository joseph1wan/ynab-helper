"""Drain manually pasted Target invoice .txt files into cached order JSON.

Workflow: copy an invoice page's rendered text (select-all + copy) into
inbox/*.txt, then run `ynab-helper import-invoices`.
Each file is parsed by invoice_text.parse_invoice_text, written as
data/target-orders/{order_id}_{invoice_id}.json — the same filename and dict
shape scrape_target_orders() writes — and archived to
data/target-orders/pasted/invoice_{order_id}_{invoice_id}.txt so it can be
re-parsed later without re-copying from Target. Files that fail to parse are
left in the inbox untouched. Invoices paid entirely by gift card/coupon (no
card_total) are archived but produce no order JSON — see ImportSkip.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ynab_helper.invoice_text import parse_invoice_text, parsed_invoice_order_date


@dataclass
class ImportedInvoice:
    source: Path
    order_id: str
    invoice_id: str
    output_path: Path
    item_count: int
    total: int


@dataclass
class ImportFailure:
    source: Path
    reason: str


@dataclass
class ImportSkip:
    source: Path
    order_id: str
    invoice_id: str
    reason: str


@dataclass
class ImportReport:
    imported: list[ImportedInvoice] = field(default_factory=list)
    failed: list[ImportFailure] = field(default_factory=list)
    skipped: list[ImportSkip] = field(default_factory=list)


def _order_json_dict(order_id: str, invoice_id: str, order_date_iso: str, total: int, items) -> dict:
    return {
        "order_id": f"{order_id}_{invoice_id}",
        "order_date": order_date_iso,
        "total": total,
        "tax": 0,
        "shipping": 0,
        "fees": 0,
        "line_items": [
            {"name": li.name, "quantity": li.quantity, "line_total": li.line_total}
            for li in items
        ],
    }


def import_pasted_invoices(
    inbox_dir: Path,
    archive_dir: Path,
    output_dir: Path,
    files: list[Path] | None = None,
    keep: bool = False,
) -> ImportReport:
    """Parse pasted invoice .txt files and write them into output_dir.

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

        parsed = parse_invoice_text(text)
        if parsed is None:
            report.failed.append(
                ImportFailure(source=path, reason="could not find order/invoice id, date, total, or items")
            )
            continue

        try:
            order_date = parsed_invoice_order_date(parsed)
        except ValueError as exc:
            report.failed.append(ImportFailure(source=path, reason=f"could not parse invoice date: {exc}"))
            continue

        if parsed.card_total == 0:
            # Paid entirely by gift card / coupon / adjustment — nothing will
            # ever post as a YNAB transaction for this invoice, so there's no
            # order JSON to write; it would just sit as permanently unmatched.
            if not keep:
                archive_path = archive_dir / f"invoice_{parsed.order_id}_{parsed.invoice_id}.txt"
                path.replace(archive_path)
            report.skipped.append(
                ImportSkip(
                    source=path,
                    order_id=parsed.order_id,
                    invoice_id=parsed.invoice_id,
                    reason="paid entirely by gift card/coupon — no card charge to match",
                )
            )
            continue

        out_path = output_dir / f"{parsed.order_id}_{parsed.invoice_id}.json"
        payload = _order_json_dict(
            parsed.order_id, parsed.invoice_id, order_date.isoformat(), parsed.card_total, parsed.line_items
        )
        with out_path.open("w") as f:
            json.dump(payload, f, indent=2)

        if not keep:
            archive_path = archive_dir / f"invoice_{parsed.order_id}_{parsed.invoice_id}.txt"
            path.replace(archive_path)

        report.imported.append(
            ImportedInvoice(
                source=path,
                order_id=parsed.order_id,
                invoice_id=parsed.invoice_id,
                output_path=out_path,
                item_count=len(parsed.line_items),
                total=parsed.card_total,
            )
        )

    return report
