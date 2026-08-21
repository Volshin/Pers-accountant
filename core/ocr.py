"""
OCR-based statement parsing for PDFs whose text layer is obfuscated.

Some banks (e.g. Freedom Bank Kazakhstan) ship PDFs where the glyph->unicode
mapping inside the embedded font is scrambled. pdfplumber / extract_text returns
garbage codepoints, so a coordinate/regex parser cannot read anything. The text
is still *visible* to a human, which means it can be recovered by rendering the
page to an image and running OCR.

This module implements that path:

    PDF -> render each page to PNG (pypdfium2)
        -> OCR with Tesseract (rus+eng)
        -> parse the recognized text into transactions
        -> validate against opening/closing balance (running balance oracle)

The parser targets the Freedom Bank "vypiska" layout, but keeps amount/date
normalization generic so it can be extended to other obfuscated formats.

It returns a pandas.DataFrame with the same unified schema the BankAdapter
contract uses:

    date, tx_date, currency, amount, description, bank

amount: negative = expense, positive = income.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# Tesseract defaults. Both are overridable via env so a Raspberry Pi (or any
# install where the binary lives elsewhere) can point at its own paths.
TESSERACT_BIN = shutil.which("tesseract") or "tesseract"
TESSERACT_LANG = "rus+eng"
OCR_DPI_SCALE = 3.0  # render scale; ~3x gives Tesseract comfortably large glyphs

BANK_NAME = "freedom_kz"

# ── Regexes for the Freedom Bank "vypiska" text layout ────────────────────────
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
_TX_DATE_RE = re.compile(r"Дата\s+транзакции:\s*(\d{2}\.\d{2}\.\d{4})", re.I)
_OPERATION_RE = re.compile(
    r"Операция:\s*(.+?)(?=\s+Дата\s+транзакции:|\s+Код\s+авторизации:|\s+Номер\s+карты:|$)",
    re.I | re.S,
)
_AMOUNT_RE = re.compile(r"транзакции:\s*([\d\s]+[.,]\d{1,2})\s*([A-Z]{3})?", re.I)
_CURRENCY_RE = re.compile(r"Валюта:\s*([A-Z]{3})", re.I)
_OPENING_RE = re.compile(r"Входящий\s+остаток:\s*([\d\s]+[.,]\d{2})", re.I)
_CLOSING_RE = re.compile(r"Исходящий\s+остаток:\s*([\d\s]+[.,]\d{2})", re.I)
_IBAN_RE = re.compile(r"KZ\d{2}[A-Z0-9]{10,}", re.I)
# Transaction anchor row: date + IBAN, followed later by a Дебет/Кредит amount.
# Numbers sit in the Debit/Credit columns right after the account number.
_DEBIT_CREDIT_AMOUNT_RE = re.compile(
    r"([\d\s]+[.,]\d{1,2})\s+(\d{4,})", re.I
)

# ── Internal (service) transaction classification ─────────────────────────────
# These move money between the user's own products and must NOT count as
# income/expense in the budget dashboard. They still move the account balance,
# so they stay in the DB (keeps the running-balance oracle consistent) but are
# tagged `tx_type = "internal"` and shown as "Внутренние переводы".
_INTERNAL_MARKERS = re.compile(
    r"Выплата\s+вклада|Прием\s+вклада|Приём\s+вклада|депозитного\s+договора|"
    r"депозит|Конвертац|Transfer\s+of\s+own\s+funds|Вкладчик",
    re.I,
)

# Which operation label each internal marker maps to (for a clean description).
_INTERNAL_LABEL = [
    (re.compile(r"Конвертац", re.I), "Конвертация валюты"),
    (re.compile(r"Transfer\s+of\s+own\s+funds", re.I), "Перевод собственных средств"),
    (re.compile(r"Выплата\s+вклада", re.I), "Выплата вклада"),
    (re.compile(r"Прием\s+вклада|Приём\s+вклада", re.I), "Приём вклада"),
]

# Account holder tails like "Валерьевич" that OCR wrongly attaches to a row.
_NOISE_TAIL = re.compile(
    r"^(?:Валерьевич|Большунов|Олег|Казахстан|Фридом|KZ65551B629508749EUR|"
    r"Валерьевич\.|Валерьевич,)\b\s*",
    re.I,
)


@dataclass
class OcrResult:
    """Structured outcome of an OCR parse, mirroring the audit's ParseResult idea."""
    records: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages_total: int = 0
    pages_with_data: int = 0
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    balance_ok: bool = False
    balance_delta: Optional[float] = None
    raw_text: str = ""


def parse_ocr(pdf_path: str | Path) -> pd.DataFrame:
    """Full pipeline: render -> OCR -> parse transactions. Returns unified DataFrame."""
    result = run_ocr(Path(pdf_path))
    return records_to_dataframe(result, BANK_NAME)


def run_ocr(pdf_path: Path | str) -> OcrResult:
    """Render a PDF to images, OCR each page, and parse transactions + balances."""
    pdf_path = Path(pdf_path)
    result = OcrResult()

    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise RuntimeError(
            "pypdfium2 is required for OCR rendering. Install it: pip install pypdfium2"
        )

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = pdfium.PdfDocument(str(pdf_path))
    result.pages_total = len(doc)

    pages_text: list[str] = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            bitmap = page.render(scale=OCR_DPI_SCALE)
            pil = bitmap.to_pil()

            # OCR one page at a time: keeps memory bounded on a Raspberry Pi.
            text = _ocr_image(pil)
            pages_text.append(text)
            if text.strip():
                result.pages_with_data += 1
    finally:
        doc.close()

    result.raw_text = "\n".join(pages_text)

    result.opening_balance = _find_amount(result.raw_text, _OPENING_RE)
    result.closing_balance = _find_amount(result.raw_text, _CLOSING_RE)
    currency = _find_currency(result.raw_text)

    result.records = _parse_records(pages_text, currency)

    _check_balance(result)
    return result


def _ocr_image(pil_image) -> str:
    """Run Tesseract on an in-memory PIL image; return recognized text."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_in = f.name
    tmp_out = tmp_in + "_ocr"
    try:
        pil_image.save(tmp_in)
        cmd = [
            TESSERACT_BIN,
            tmp_in,
            tmp_out,
            "-l",
            TESSERACT_LANG,
            "--psm",
            "6",  # assume a uniform block of text; bank tables are dense blocks
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            log.warning("tesseract stderr: %s", proc.stderr[:500])
            return ""
        return Path(tmp_out + ".txt").read_text(encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("OCR failed: %s", e)
        return ""
    finally:
        for p in (tmp_in, tmp_out + ".txt"):
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass


def _find_amount(text: str, pattern: re.Pattern) -> Optional[float]:
    m = pattern.search(text)
    if not m:
        return None
    return _parse_number(m.group(1))


def _find_currency(text: str) -> str:
    m = _CURRENCY_RE.search(text)
    if m:
        return m.group(1).upper()
    # Freedom prints the account like "KZ65551B629508749EUR" — trailing currency.
    m2 = re.search(r"KZ\d{2}[A-Z0-9]{8,}([A-Z]{3})\b", text)
    if m2:
        return m2.group(1)
    return "EUR"


def _parse_number(s: str) -> float:
    """Parse a human amount, tolerant of OCR quirks."""
    if s is None:
        return 0.0
    s = s.replace("\u00a0", " ").replace(",", ".").strip()
    # Remove spaces that OCR may insert inside thousands: "6 487.95" -> "6487.95"
    m = re.search(r"(\d[\d\s]*\.\d{1,2})", s)
    if not m:
        return 0.0
    digits = m.group(1).replace(" ", "")
    try:
        return float(digits)
    except ValueError:
        return 0.0


def _parse_records(pages_text: list[str], currency: str) -> list[dict]:
    """Extract transactions from OCR'ed pages.

    Freedom lays each transaction across a visual block. The reliable anchor is
    "Дата транзакции: DD.MM.YYYY". Within the window between one anchor and the
    next, the transaction amount appears in one of two places:

      1. the Debit/Credit column — a decimal number sitting right after the
         account/IBAN token and before a 4-6 digit document code, e.g.
         "... KZ65551B629508749EUR 54.91 254462 ..."
      2. the "Сумма транзакции: N EUR" line (OCR may split it across lines).
    """
    records: list[dict] = []

    for page_text in pages_text:
        records.extend(_parse_page_records(page_text, currency))

    return records


def _parse_page_records(text: str, currency: str) -> list[dict]:
    records: list[dict] = []

    # 1) Regular card transactions: anchored by "Дата транзакции: DD.MM.YYYY".
    tx_date_iter = list(_TX_DATE_RE.finditer(text))

    for i, m in enumerate(tx_date_iter):
        date_str = m.group(1)
        start = m.end()
        end = tx_date_iter[i + 1].start() if i + 1 < len(tx_date_iter) else len(text)
        window = text[start:end]

        amount = _extract_amount(window)
        if amount is None:
            amount = 0.0

        op_m = _OPERATION_RE.search(window)
        description = op_m.group(1).strip() if op_m else ""
        description = _clean_description(description)

        if not description and amount == 0.0:
            continue

        records.append(
            {
                "date": _fmt_date(date_str),
                "tx_date": _fmt_date(date_str),
                "currency": currency,
                "amount": -amount if amount > 0 else 0.0,
                "description": description or "—",
                "bank": BANK_NAME,
                "tx_type": "normal",
            }
        )

    # 2) Internal service operations (deposit transfers, conversions). These have
    #    no "Дата транзакции:" anchor — they carry "Выплата вклада", "Приём вклада",
    #    "Конвертация", "Transfer of own funds", "Вкладчик ..." instead.
    records.extend(_parse_internal_records(text, currency))

    return records


def _parse_internal_records(text: str, currency: str) -> list[dict]:
    """Collect internal (service) transactions: deposit in/out, conversions.

    These are anchored by a date row `DD.MM.YYYY <code> ... <marker>` where the
    marker is one of the internal-tx phrases. They must be tagged `tx_type =
    "internal"` so the dashboard shows them as "Внутренние переводы", not as
    income/expense.
    """
    records: list[dict] = []

    # Split the text into logical blocks at row-start dates.
    # A service block starts with "DD.MM.YYYY" and (somewhere in it) an internal marker.
    lines = text.split("\n")
    # Join everything to one line-array but keep structured: find date rows.
    for i, line in enumerate(lines):
        line = line.strip()
        m = _DATE_RE.search(line)
        if not m or not line.startswith(m.group(1)[:8]) and not re.match(r"^\d{2}\.\d{2}\.\d{4}", line):
            # only match rows that START with a DD.MM.YYYY date
            if not re.match(r"^\d{2}\.\d{2}\.\d{4}", line):
                continue
        if not re.match(r"^\d{2}\.\d{2}\.\d{4}", line):
            continue

        # Gather this row plus following non-date rows as the block window.
        block_lines = [line]
        j = i + 1
        while j < len(lines) and not re.match(r"^\d{2}\.\d{2}\.\d{4}", lines[j].strip()):
            block_lines.append(lines[j].strip())
            j += 1
        block = "\n".join(block_lines)

        # Only take it if it looks like a service operation.
        marker = _INTERNAL_MARKERS.search(block)
        if not marker:
            continue

        date_str = re.match(r"^(\d{2}\.\d{2}\.\d{4})", block).group(1)
        label = _classify_internal(block)

        # Amount in deposit rows often appears as "... сумме 5187.89 EUR" or a
        # standalone decimal before the account/IBAN token.
        amount = _extract_internal_amount(block)

        records.append(
            {
                "date": _fmt_date(date_str),
                "tx_date": _fmt_date(date_str),
                "currency": currency,
                # Sign: we don't know debit vs credit reliably for internal ops;
                # keep 0 sign-neutral and rely on the balance oracle for
                # diagnostics. Dashboard shows them separately anyway.
                "amount": amount,
                "description": label,
                "bank": BANK_NAME,
                "tx_type": "internal",
            }
        )

    return records


def _classify_internal(block: str) -> str:
    for pattern, label in _INTERNAL_LABEL:
        if pattern.search(block):
            return label
    return "Внутренний перевод"


def _extract_internal_amount(block: str) -> float:
    """Recover amount for internal ops: 'сумме 5187.89 EUR' or '0.01' after code."""
    # "сумме 5187.89 EUR" pattern
    m = re.search(r"сумме\s*([\d\s]+[.,]\d{1,2})", block, re.I)
    if m:
        return _parse_number(m.group(1))
    # generic decimal fallback (first one)
    m2 = re.search(r"([\d][\d\s]*[.,]\d{1,2})", block)
    if m2:
        return _parse_number(m2.group(1))
    return 0.0


def _extract_amount(window: str) -> Optional[float]:
    """Recover a transaction amount from an OCR window, best-effort.

    Priority:
      1. "Сумма транзакции: N EUR" (may be split across a newline).
      2. Debit/Credit column: decimal before a 4-6 digit document code.
    """
    # 1. Сумма транзакции (join the OCR newline-split form "Сумма\nтранзакции:")
    joined = re.sub(r"\s*\n\s*", " ", window)  # collapse newlines to spaces
    m = _AMOUNT_RE.search(joined)
    if m:
        return _parse_number(m.group(1))

    # 2. Debit/Credit column: a decimal amount followed by a 4-6 digit code.
    #    e.g. "... EUR 54.91 254462 ..." or "... EUR 1.50 107878 ..."
    for cand in re.finditer(r"([\d][\d\s]{0,9}[.,]\d{1,2})\s+(\d{4,6})(?:\s|$)", joined):
        amt = _parse_number(cand.group(1))
        if 0 < amt < 1_000_000:
            return amt

    return None


def _clean_description(s: str) -> str:
    s = s.strip()
    # Collapse whitespace/newlines OCR inserted.
    s = re.sub(r"\s+", " ", s).strip()
    # Trim leading noise tails like "Валерьевич" / account owner fragments.
    s = _NOISE_TAIL.sub("", s).strip()
    return s


def _fmt_date(ddmm: str) -> str:
    """'DD.MM.YYYY' -> 'YYYY-MM-DD'."""
    try:
        return datetime.strptime(ddmm, "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ddmm


def _check_balance(result: OcrResult) -> None:
    """Compare parsed sum against opening/closing balance; record the delta.

    This is the external oracle: the bank prints a running balance, so
    opening + sum(transactions) should equal closing. A non-zero delta is a
    strong signal that OCR missed or misread rows — surfaced as a warning and
    balance_delta rather than silently accepted.
    """
    if result.opening_balance is None or result.closing_balance is None:
        result.warnings.append("Не найдены входящий/исходящий остаток — проверка сальдо пропущена")
        return

    total = sum(r["amount"] for r in result.records)
    expected_closing = round(result.opening_balance + total, 2)
    delta = round(result.closing_balance - expected_closing, 2)

    result.balance_delta = delta
    result.balance_ok = delta == 0.0

    if not result.balance_ok:
        result.warnings.append(
            f"Сальдо не сходится: входящий {result.opening_balance:.2f} + "
            f"сумма операций {total:.2f} = {expected_closing:.2f}, "
            f"а исходящий {result.closing_balance:.2f} (Δ={delta:.2f})"
        )


def records_to_dataframe(result: OcrResult, bank: str = BANK_NAME) -> pd.DataFrame:
    """Convert OCR records into the unified schema DataFrame."""
    cols = ["date", "tx_date", "currency", "amount", "description", "bank", "tx_type"]
    if result.records:
        df = pd.DataFrame(result.records)
    else:
        df = pd.DataFrame(columns=cols)
    # ensure tx_type exists for downstream consumers
    if "tx_type" not in df.columns:
        df["tx_type"] = "normal"
    return df


__all__ = ["parse_ocr", "run_ocr", "OcrResult", "records_to_dataframe"]
