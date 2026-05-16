#!/usr/bin/env python3
"""
Watchdog service: monitors inbox/ for new PDF files, processes them,
and writes a text summary to reports/.

    ~/finance/
    ├── inbox/    ← drop PDFs here (Samba share)
    ├── reports/  ← summaries appear here automatically
    └── done/     ← processed PDFs archived here
"""
import os
import sys
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from core.categorizer import categorize_all
from core.db import insert_transactions, record_import
from core.detector import detect_adapter

BASE_DIR    = Path(os.environ.get("FINANCE_DIR", Path.home() / "finance"))
INBOX_DIR   = BASE_DIR / "inbox"
REPORTS_DIR = BASE_DIR / "reports"
DONE_DIR    = BASE_DIR / "done"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "watcher.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

_in_progress: set[str] = set()
_lock = threading.Lock()


def _is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf" and not path.name.startswith(".")


def process_pdf(pdf_path: Path) -> None:
    with _lock:
        if pdf_path.name in _in_progress:
            return
        _in_progress.add(pdf_path.name)

    log.info(f"{'─' * 52}")
    log.info(f"Файл:       {pdf_path.name}")

    try:
        adapter = detect_adapter(str(pdf_path))
        log.info(f"Банк:       {adapter.bank_name}")

        df = adapter.parse(str(pdf_path))
        if df.empty:
            log.warning("Транзакции не найдены — файл пропущен")
            return

        log.info(f"Транзакций: {len(df)}")
        log.info("Категоризирую через Ollama…")
        df["category"] = categorize_all(df["description"].tolist())

        import_id = str(uuid4())
        inserted, skipped = insert_transactions(df, import_id)
        log.info(f"Сохранено:  {inserted} новых, {skipped} дублей пропущено")
        if inserted > 0:
            record_import(import_id, pdf_path.name, adapter.bank_name, inserted)
            log.info(f"Import ID:  {import_id}")

        report_path = _write_report(pdf_path.stem, df, adapter.bank_name)
        log.info(f"Отчёт:      {report_path.name}")
        _log_summary(df)

        shutil.move(str(pdf_path), str(DONE_DIR / pdf_path.name))
        log.info(f"Файл перемещён в done/")

    except Exception as e:
        log.error(f"Ошибка: {e}", exc_info=True)
    finally:
        with _lock:
            _in_progress.discard(pdf_path.name)


def _write_report(stem: str, df, bank: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"{timestamp}_{stem}.txt"

    lines = [
        "=" * 52,
        f"  Файл:  {stem}",
        f"  Банк:  {bank}",
        f"  Дата:  {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "=" * 52,
    ]
    lines.extend(_summary_lines(df))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _summary_lines(df) -> list[str]:
    lines = []
    expenses = df[df["amount"] < 0]
    income   = df[df["amount"] > 0]

    for cur, group in expenses.groupby("currency"):
        lines.append(f"\nРасходы ({cur})")
        lines.append("─" * 38)
        for cat, total in group.groupby("category")["amount"].sum().sort_values().items():
            lines.append(f"  {cat:<20} {total:>9.2f} {cur}")
        lines.append(f"  {'ИТОГО':<20} {group['amount'].sum():>9.2f} {cur}")

    for cur, group in income.groupby("currency"):
        lines.append(f"\nДоходы ({cur})")
        lines.append("─" * 38)
        for cat, total in group.groupby("category")["amount"].sum().sort_values(ascending=False).items():
            lines.append(f"  {cat:<20} {total:>9.2f} {cur}")
        lines.append(f"  {'ИТОГО':<20} {group['amount'].sum():>9.2f} {cur}")

    lines.append(f"\n  Всего транзакций: {len(df)}")
    lines.append("=" * 52)
    return lines


def _log_summary(df) -> None:
    for line in _summary_lines(df):
        if line.strip():
            log.info(line)


class PDFHandler(FileSystemEventHandler):
    def on_closed(self, event) -> None:
        """Fires on Linux when the file is fully written and closed (inotify IN_CLOSE_WRITE)."""
        if event.is_directory:
            return
        path = Path(event.src_path)
        if _is_pdf(path):
            process_pdf(path)


def main() -> None:
    for d in (INBOX_DIR, REPORTS_DIR, DONE_DIR):
        d.mkdir(parents=True, exist_ok=True)

    log.info(f"Слежу за папкой {INBOX_DIR}")
    log.info(f"Отчёты → {REPORTS_DIR}")

    for pdf in sorted(INBOX_DIR.glob("*.pdf")):
        if _is_pdf(pdf):
            process_pdf(pdf)

    observer = Observer()
    observer.schedule(PDFHandler(), str(INBOX_DIR), recursive=False)
    observer.start()
    try:
        while observer.is_alive():
            observer.join(timeout=5)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
