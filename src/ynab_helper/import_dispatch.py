"""Unified drain for the single top-level inbox/ directory.

Workflow: paste Target invoices to inbox/target_N.txt, Costco receipts to
inbox/costco_N.txt, Amazon orders to inbox/amazon_N.txt (see the pb_target /
pb_costco shell aliases), and drop PayPal activity CSV exports in as
inbox/*.csv — any filename, PayPal is the only source that arrives as a CSV.
Then run `ynab-helper import-invoices` with no arguments to dispatch every
file in inbox/ to the right parser by filename convention:

- *.csv                 -> paypal_csv.import_paypal_csvs
- target_*.txt          -> invoice_import.import_pasted_invoices
- costco_*.txt          -> costco_import.import_pasted_receipts
- amazon_*.txt          -> amazon_import.import_pasted_amazon_orders

Anything else left in inbox/ (wrong prefix, wrong extension) is reported as
a failure and left in place rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ynab_helper.amazon_import import import_pasted_amazon_orders
from ynab_helper.costco_import import import_pasted_receipts
from ynab_helper.invoice_import import import_pasted_invoices
from ynab_helper.paypal_csv import import_paypal_csvs


@dataclass
class DispatchFailure:
    source: Path
    reason: str


@dataclass
class DispatchReport:
    target_imported: int = 0
    target_skipped: int = 0
    costco_imported: int = 0
    amazon_imported: int = 0
    paypal_imported: int = 0
    paypal_new_records: int = 0
    lines: list[str] = field(default_factory=list)
    failed: list[DispatchFailure] = field(default_factory=list)


def _sort_inbox_files(inbox_dir: Path, files: list[Path] | None) -> list[Path]:
    return sorted(files) if files is not None else sorted(inbox_dir.glob("*"))


def import_inbox(
    inbox_dir: Path,
    target_orders_dir: Path,
    target_archive_dir: Path,
    costco_orders_dir: Path,
    costco_archive_dir: Path,
    paypal_records_path: Path,
    paypal_archive_dir: Path,
    amazon_orders_dir: Path,
    amazon_archive_dir: Path,
    files: list[Path] | None = None,
    keep: bool = False,
) -> DispatchReport:
    report = DispatchReport()

    candidates = [p for p in _sort_inbox_files(inbox_dir, files) if p.is_file()]

    target_files: list[Path] = []
    costco_files: list[Path] = []
    paypal_files: list[Path] = []
    amazon_files: list[Path] = []

    for path in candidates:
        name = path.name.lower()
        if path.suffix.lower() == ".csv":
            paypal_files.append(path)
        elif name.startswith("target_") and path.suffix.lower() == ".txt":
            target_files.append(path)
        elif name.startswith("costco_") and path.suffix.lower() == ".txt":
            costco_files.append(path)
        elif name.startswith("amazon_") and path.suffix.lower() == ".txt":
            amazon_files.append(path)
        else:
            report.failed.append(
                DispatchFailure(
                    source=path,
                    reason="unrecognized filename — expected target_*.txt, costco_*.txt, amazon_*.txt, or *.csv",
                )
            )

    if target_files:
        target_report = import_pasted_invoices(
            inbox_dir=inbox_dir,
            archive_dir=target_archive_dir,
            output_dir=target_orders_dir,
            files=target_files,
            keep=keep,
        )
        report.target_imported = len(target_report.imported)
        report.target_skipped = len(target_report.skipped)
        for item in target_report.imported:
            report.lines.append(
                f"{item.source.name} -> {item.output_path.name} "
                f"({item.item_count} item{'s' if item.item_count != 1 else ''}, "
                f"${item.total / 1000:.2f})"
            )
        for skip in target_report.skipped:
            report.lines.append(f"{skip.source.name} -> skipped ({skip.reason})")
        for failure in target_report.failed:
            report.failed.append(DispatchFailure(source=failure.source, reason=failure.reason))

    if costco_files:
        costco_report = import_pasted_receipts(
            inbox_dir=inbox_dir,
            archive_dir=costco_archive_dir,
            output_dir=costco_orders_dir,
            files=costco_files,
            keep=keep,
        )
        report.costco_imported = len(costco_report.imported)
        for item in costco_report.imported:
            report.lines.append(
                f"{item.source.name} -> {item.output_path.name} "
                f"({item.item_count} item{'s' if item.item_count != 1 else ''}, "
                f"${item.total / 1000:.2f})"
            )
        for failure in costco_report.failed:
            report.failed.append(DispatchFailure(source=failure.source, reason=failure.reason))

    if amazon_files:
        amazon_report = import_pasted_amazon_orders(
            inbox_dir=inbox_dir,
            archive_dir=amazon_archive_dir,
            output_dir=amazon_orders_dir,
            files=amazon_files,
            keep=keep,
        )
        report.amazon_imported = len(amazon_report.imported)
        for item in amazon_report.imported:
            report.lines.append(
                f"{item.source.name} -> {item.output_path.name} "
                f"({item.item_count} item{'s' if item.item_count != 1 else ''}, "
                f"${item.total / 1000:.2f})"
            )
        for failure in amazon_report.failed:
            report.failed.append(DispatchFailure(source=failure.source, reason=failure.reason))

    if paypal_files:
        paypal_report = import_paypal_csvs(
            inbox_dir=inbox_dir,
            archive_dir=paypal_archive_dir,
            records_path=paypal_records_path,
            files=paypal_files,
            keep=keep,
        )
        report.paypal_imported = len(paypal_report.imported)
        report.paypal_new_records = paypal_report.new_records
        for item in paypal_report.imported:
            report.lines.append(f"{item.source.name}: {item.record_count} records")
        for failure in paypal_report.failed:
            report.failed.append(DispatchFailure(source=failure.source, reason=failure.reason))

    return report
