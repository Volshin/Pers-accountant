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
FAILED_DIR  = BASE_DIR / "failed"

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
        parsed_total = len(df)

        if df.empty:
            # Distinct from a crash: parser ran, found nothing.
            _record_failure(pdf_path, adapter.bank_name, "empty",
                            "Парсер не нашёл транзакций (файл пустой или формат не распознан).")
            return

        log.info(f"Транзакций: {parsed_total}")
        log.info("Категоризирую через Ollama…")
        tx_types = df["tx_type"].tolist() if "tx_type" in df.columns else None
        df["category"] = categorize_all(df["description"].tolist(), tx_types)

        import_id = str(uuid4())
        inserted, skipped = insert_transactions(df, import_id)
        log.info(f"Сохранено:  {inserted} новых, {skipped} дублей пропущено")

        # parsed>0 but inserted==0 means the whole file was duplicates OR a bug.
        status = "ok"
        if inserted == 0 and parsed_total > 0:
            status = "duplicate"
        record_import(import_id, pdf_path.name, adapter.bank_name,
                      inserted, parsed_total=parsed_total, skipped=skipped,
                      status=status)
        log.info(f"Import ID:  {import_id}")

        report_path = _write_report(pdf_path.stem, df, adapter.bank_name)
        log.info(f"Отчёт:      {report_path.name}")
        _log_summary(df)

        _move_to_done(pdf_path)

    except Exception as e:
        log.error(f"Ошибка: {e}", exc_info=True)
        _record_failure(pdf_path, "unknown", "error", f"{type(e).__name__}: {e}")
    finally:
        with _lock:
            _in_progress.discard(pdf_path.name)


def _record_failure(pdf_path: Path, bank: str, status: str, reason: str) -> None:
    """Move a failed PDF into failed/ (not done/) and write a diagnostics file.

    This is the fix for the 'silent swallow' problem: previously the file still
    went to done/ and the user believed the import succeeded. Now the failure is
    visible both as a failed/ artifact and via the imports.status column.
    """
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    diag_path = FAILED_DIR / f"{timestamp}_{pdf_path.stem}.txt"
    try:
        diag_path.write_text(
            f"Файл: {pdf_path.name}\nБанк: {bank}\nСтатус: {status}\nПричина: {reason}\n",
            encoding="utf-8",
        )
        log.error(f"Провал → {diag_path.name}")
    except OSError as e:
        log.error(f"Не удалось записать диагностику: {e}")

    dest = FAILED_DIR / pdf_path.name
    try:
        shutil.move(str(pdf_path), str(dest))
        log.info(f"Файл перемещён в failed/")
    except OSError:
        try:
            shutil.copy2(str(pdf_path), str(dest))
            pdf_path.unlink()
            log.info(f"Файл скопирован в failed/ и удалён из inbox")
        except OSError as e:
            log.warning(f"Не удалось переместить файл в failed/: {e}")


def _move_to_done(pdf_path: Path) -> None:
    dest = DONE_DIR / pdf_path.name
    try:
        shutil.move(str(pdf_path), str(dest))
        log.info("Файл перемещён в done/")
    except FileNotFoundError:
        log.warning("Файл не найден при перемещении — пропущено")
    except OSError:
        # Cross-device or Samba lock: copy + delete
        try:
            shutil.copy2(str(pdf_path), str(dest))
            pdf_path.unlink()
            log.info("Файл скопирован в done/ и удалён из inbox")
        except OSError as e:
            log.warning(f"Не удалось переместить файл: {e}")


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
    for d in (INBOX_DIR, REPORTS_DIR, DONE_DIR, FAILED_DIR):
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
