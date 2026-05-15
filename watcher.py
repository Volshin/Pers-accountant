#!/usr/bin/env python3
"""
Watchdog service: monitors inbox/ for new PDF files, processes them,
and writes a text summary to reports/.

Directory layout (configured via env or defaults below):
    ~/finance/
    ├── inbox/      ← drop PDFs here (Samba share)
    └── reports/    ← summaries appear here automatically
"""
import os
import sys
import time
import logging
import shutil
from datetime import datetime
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

from core.categorizer import categorize_all
from core.db import insert_transactions
from core.detector import detect_adapter

BASE_DIR = Path(os.environ.get("FINANCE_DIR", Path.home() / "finance"))
INBOX_DIR   = BASE_DIR / "inbox"
REPORTS_DIR = BASE_DIR / "reports"
DONE_DIR    = BASE_DIR / "done"     # processed PDFs moved here

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "watcher.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def process_pdf(pdf_path: Path) -> None:
    log.info(f"Processing: {pdf_path.name}")

    try:
        adapter = detect_adapter(str(pdf_path))
        log.info(f"  Adapter: {adapter.bank_name}")

        df = adapter.parse(str(pdf_path))
        if df.empty:
            log.warning(f"  No transactions parsed from {pdf_path.name}")
            return

        log.info(f"  Parsed {len(df)} transactions, categorizing…")
        df["category"] = categorize_all(df["description"].tolist())

        inserted, skipped = insert_transactions(df)
        log.info(f"  Saved {inserted} new, {skipped} duplicates skipped")

        _write_report(pdf_path.stem, df, adapter.bank_name)

        dest = DONE_DIR / pdf_path.name
        shutil.move(str(pdf_path), str(dest))
        log.info(f"  Moved to done/{pdf_path.name}")

    except Exception as e:
        log.error(f"  Failed to process {pdf_path.name}: {e}", exc_info=True)


def _write_report(stem: str, df, bank: str) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"{timestamp}_{stem}_summary.txt"

    lines = [
        f"{'='*50}",
        f"  Сводка: {stem}",
        f"  Банк:   {bank}",
        f"  Дата:   {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"{'='*50}",
    ]

    expenses = df[df["amount"] < 0]
    income   = df[df["amount"] > 0]

    for cur, group in expenses.groupby("currency"):
        lines.append(f"\nРасходы ({cur})")
        lines.append("-" * 35)
        summary = group.groupby("category")["amount"].sum().sort_values()
        for cat, total in summary.items():
            lines.append(f"  {cat:<18} {total:>10.2f} {cur}")
        lines.append(f"  {'ИТОГО':<18} {group['amount'].sum():>10.2f} {cur}")

    for cur, group in income.groupby("currency"):
        lines.append(f"\nДоходы ({cur})")
        lines.append("-" * 35)
        summary = group.groupby("category")["amount"].sum().sort_values(ascending=False)
        for cat, total in summary.items():
            lines.append(f"  {cat:<18} {total:>10.2f} {cur}")
        lines.append(f"  {'ИТОГО':<18} {group['amount'].sum():>10.2f} {cur}")

    lines.append(f"\nВсего транзакций: {len(df)}")
    lines.append("=" * 50)

    report_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"  Report written: {report_path.name}")


class PDFHandler(FileSystemEventHandler):
    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".pdf" or path.name.startswith("._"):
            return
        # Brief wait — some apps write the file in chunks
        time.sleep(1)
        if path.exists():
            process_pdf(path)


def main() -> None:
    for d in (INBOX_DIR, REPORTS_DIR, DONE_DIR):
        d.mkdir(parents=True, exist_ok=True)

    log.info(f"Watching {INBOX_DIR} for PDF files…")
    log.info(f"Reports → {REPORTS_DIR}")

    # Process any PDFs already sitting in inbox (e.g. after restart)
    for pdf in sorted(INBOX_DIR.glob("*.pdf")):
        process_pdf(pdf)

    observer = Observer()
    observer.schedule(PDFHandler(), str(INBOX_DIR), recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
