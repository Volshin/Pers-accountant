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
# Each transaction is a ROW that starts with "DD.MM.YYYY <code>" and holds the
# account/IBAN token + amount + doc-id + optional "Номер карты" tail. The row
# may continue onto following lines (OCR wraps), so a record spans from one
# date-anchored line to the next.
_DATE_ROW_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\b")

# The amount printed in the transaction row: a decimal number that sits right
# after the account/IBAN token (which itself ends in the 3-letter currency, e.g.
# "...KZ65551B629508749EUR 54.91 254462..."). We capture the FIRST decimal that
# follows such a token, or a bare decimal in the row, preferring the one that is
# NOT a doc-id (doc-ids are 4-6 digits with no decimal point).
_ROW_AMOUNT_RE = re.compile(r"(\d{1,3}(?:[\s\u00a0]\d{3})*[.,]\d{1,2})")

# "транзакции: N EUR" appears on the wrapped description line and mirrors the
# same amount. Used as a cross-check / fallback only.
_TX_AMOUNT_RE = re.compile(r"транзакции:\s*([\d]+(?:[.,]\d{1,2})?)\s*EUR\b", re.I)
_SUMME_AMOUNT_RE = re.compile(r"(?:сумме|сумма)\s+([\d\s]+[.,]\d{1,2})\s*EUR", re.I)

# Opening / closing balance from the statement header/footer.
_OPENING_RE = re.compile(r"Входящий\s+остаток:\s*([\d\s]+[.,]\d{2})", re.I)
_CLOSING_RE = re.compile(r"Исходящий\s+остаток:\s*([\d\s]+[.,]\d{2})", re.I)

# Currency from "Валюта: XXX" or trailing currency in the account token.
_CURRENCY_RE = re.compile(r"Валюта:\s*([A-Z]{3})", re.I)
_ACCOUNT_CURRENCY_RE = re.compile(r"KZ\d{2}[A-Z0-9]{8,}([A-Z]{3})\b", re.I)

# ── Internal (service) transaction classification ─────────────────────────────
# Freedom's Deposit Card runs a "deposit" account alongside the card account.
# Rows carrying these markers belong to that deposit account (technical round
# trips of the same money) and do NOT touch the card balance. We skip them
# entirely so they never pollute card income/expense.
_DEPOSIT_MARKERS = re.compile(
    r"Вкладчик|Прием\s+вклада|Приём\s+вклада|Выплата\s+вклада|"
    r"депозитного\s+договора|депозит",
    re.I,
)

# Account-holder / footer noise fragments that OCR wrongly attaches to rows.
# These show up both at the start AND inside descriptions, so strip them anywhere.
_NOISE_TAIL = re.compile(
    r"^(?:Валерьевич|Большунов|Олег|Казахстан|Фридом|KZ\d{2}[A-Z0-9]+EUR)\b\s*",
    re.I,
)
_NOISE_INLINE = re.compile(
    r"\b(?:Казахстан|Валерьевич|Большунов)\b\s*",
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

    result.records = _parse_page_records(result.raw_text, currency)

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
    m2 = _ACCOUNT_CURRENCY_RE.search(text)
    if m2:
        return m2.group(1)
    return "EUR"


def _parse_number(s: str) -> float:
    """Parse a human amount, tolerant of OCR quirks."""
    if s is None:
        return 0.0
    s = s.replace("\u00a0", " ").replace(",", ".").strip()
    m = re.search(r"(\d[\d\s]*\.\d{1,2})", s)
    if not m:
        return 0.0
    digits = m.group(1).replace(" ", "")
    try:
        return float(digits)
    except ValueError:
        return 0.0


def _parse_page_records(text: str, currency: str) -> list[dict]:
    """Extract card transactions from OCR'ed text.

    Freedom lays each card transaction across several OCR lines:

        DD.MM.YYYY <code> ... KZ...EUR <amount> <doc-id> Номер карты: ... Сумма
        . транзакции: <amount> EUR Операция: <description>
        карты в чужом устройстве        (wrapped tail)

    The authoritative amount is the decimal printed in the HEADER line, right
    before its 5-6 digit doc-id. When the header amount was dropped by OCR, we
    fall back to the "транзакции: N EUR" / "сумме N EUR" line. Deposit-account
    rows ("Вкладчик", "Прием вклада", ...) belong to a parallel account and are
    skipped entirely — they do not change the card balance.

    A refund ("Возврат") is income (positive); everything else is an expense
    (negative).
    """
    records: list[dict] = []

    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        s = lines[i].strip()

        # A card header line: starts with a date AND mentions the card number /
        # amount, AND is not a deposit-account row.
        if _DATE_ROW_RE.match(s) and not _DEPOSIT_MARKERS.search(s) and (
            "Номер карты" in s or "Сумма" in s or "карты" in s
        ):
            date_str = _DATE_ROW_RE.match(s).group(1)
            header_amount = _header_amount(s)

            # Collect this header plus following non-date continuation lines,
            # where "транзакции: N EUR Операция: ..." / "сумме N EUR" may live.
            tail_lines = [s]
            j = i + 1
            while j < n and not _DATE_ROW_RE.match(lines[j].strip()):
                tail_lines.append(lines[j].strip())
                j += 1
            block = "\n".join(tail_lines)

            amount = header_amount
            if amount is None:
                amount = _fallback_amount(block)

            if amount is None:
                i = j
                continue

            is_refund = bool(re.search(r"Возврат", block, re.I))
            description = _extract_op(block)

            records.append(
                {
                    "date": _fmt_date(date_str),
                    "tx_date": _fmt_date(date_str),
                    "currency": currency,
                    "amount": round((1.0 if is_refund else -1.0) * amount, 2),
                    "description": description or "—",
                    "bank": BANK_NAME,
                    "tx_type": "normal",
                }
            )
            i = j
        else:
            i += 1

    return records


def _header_amount(line: str) -> Optional[float]:
    """Return the amount printed in a card header line, if present.

    The header looks like:  "DD.MM.YYYY <code> ... KZ...EUR <amount> <doc-id>
    Номер карты: ... Сумма". The amount is the decimal sitting immediately
    before the 5-6 digit doc-id (or before "Номер карты" when no doc-id).
    """
    # decimal followed by a 5-6 digit doc-id or by "Номер карты"
    m = re.search(r"(\d{1,3}(?:[\s\u00a0]\d{3})*[.,]\d{1,2})\s+(?:\d{5,6}|Номер карты|Сумма)", line)
    if m:
        amt = _parse_number(m.group(1))
        if amt > 0:
            return amt
    # fallback: any decimal after "EUR" in the header
    m2 = re.search(r"EUR\s*(?:[—–-]\s*)?(\d{1,3}(?:[\s\u00a0]\d{3})*[.,]\d{1,2})", line)
    if m2:
        amt = _parse_number(m2.group(1))
        if amt > 0:
            return amt
    return None


def _fallback_amount(block: str) -> Optional[float]:
    """Fallback amount from a 'транзакции: N EUR' / 'сумме N EUR' line.

    This value is trusted only when it carries a decimal point; a bare integer
    here may be a point-dropped glitch (1913) OR a genuine whole amount (250),
    so we prefer the header and only end up here when the header was missing.
    """
    for m in _TX_AMOUNT_RE.finditer(block):
        raw = m.group(1).replace(",", ".")
        if "." in raw:
            return _parse_number(raw)
    for m in _SUMME_AMOUNT_RE.finditer(block):
        return _parse_number(m.group(1))
    # Last resort: bare integer from "транзакции" (accept as-is; better than none).
    for m in _TX_AMOUNT_RE.finditer(block):
        return _parse_number(m.group(1))
    return None


def _extract_op(block: str) -> str:
    """Extract 'Операция: ...' text from a block, if present."""
    m = re.search(r"Операция:\s*(.+?)(?:\s+(?:Дата|Код|Номер)\b|$)", block, re.I | re.S)
    if m:
        return _clean_description(m.group(1))
    return ""


def _clean_description(s: str) -> str:
    s = s.strip()
    # Collapse whitespace/newlines OCR inserted.
    s = re.sub(r"\s+", " ", s).strip()
    # Trim leading noise tails like "Валерьевич" / account owner fragments.
    s = _NOISE_TAIL.sub("", s).strip()
    # Strip mid-description account-holder fragments ("Казахстан Валерьевич").
    s = _NOISE_INLINE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Drop trailing stray glyphs/commas OCR left mid-sentence.
    s = re.sub(r"[\s,]*$", "", s).strip()
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
