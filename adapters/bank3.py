"""
Bank 3 adapter — Freedom Bank Kazakhstan (KZ), current account statement.
Table: Дата операции | Номер документа | ... | Дебет | Кредит | Назначение платежа
"""
import re
from datetime import datetime
import pdfplumber
import pandas as pd
from .base import BankAdapter

BANK_NAME = "freedom_kz"

_DATE_RE     = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_AMOUNT_RE   = re.compile(r"^\d[\d,]*\.\d{2}$")
_IBAN_RE     = re.compile(r"^KZ[A-Z0-9]{10,}$")
_TX_DATE_RE  = re.compile(r"Дата транзакции:\s*(\d{2}\.\d{2}\.\d{4})")
_OPERACIA_RE = re.compile(r"Операция:\s*(.+)")
_CURRENCY_RE = re.compile(r"Валюта:\s*([A-Z]{3})")


def _fmt(d: str) -> str:
    return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")


def _parse_amount(s: str) -> float:
    return float(s.replace(",", ""))


class Bank3Adapter(BankAdapter):
    bank_name = BANK_NAME

    @classmethod
    def can_handle(cls, first_page_text: str) -> bool:
        return "FREEDOM BANK KAZAKHSTAN" in first_page_text

    def parse(self, pdf_path: str) -> pd.DataFrame:
        currency = _detect_currency(pdf_path)
        records: list[dict] = []
        debit_x: float | None = None
        credit_x: float | None = None

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words()
                if not words:
                    continue

                # Locate Дебет / Кредит column centres from header (once per statement)
                for w in words:
                    if w["text"] == "Дебет" and debit_x is None:
                        debit_x = (w["x0"] + w["x1"]) / 2
                    elif w["text"] == "Кредит" and credit_x is None:
                        credit_x = (w["x0"] + w["x1"]) / 2

                if debit_x is None or credit_x is None:
                    continue

                records.extend(_parse_page(words, debit_x, credit_x, currency))

        return pd.DataFrame(records)


def _detect_currency(pdf_path: str) -> str:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = pdf.pages[0].extract_text() or ""
        m = _CURRENCY_RE.search(text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "EUR"


def _parse_page(
    words: list[dict], debit_x: float, credit_x: float, currency: str
) -> list[dict]:
    # Bucket words into visual rows by Y position (3pt tolerance)
    rows: dict[int, list[dict]] = {}
    for w in words:
        y_key = round(w["top"] / 3) * 3
        rows.setdefault(y_key, []).append(w)

    records: list[dict] = []
    current: dict | None = None
    past_iban = False

    for y_key in sorted(rows):
        row = sorted(rows[y_key], key=lambda w: w["x0"])
        texts = [w["text"] for w in row]

        # New transaction starts with DD.MM.YYYY in the first cell
        if _DATE_RE.match(texts[0]):
            if current is not None and current["amount"] is not None:
                _finalize(current)
                records.append(current)
            current = {
                "date":        _fmt(texts[0]),
                "tx_date":     _fmt(texts[0]),
                "currency":    currency,
                "amount":      None,
                "description": "",
                "bank":        BANK_NAME,
            }
            past_iban = False
            continue

        if current is None:
            continue

        # Row containing the beneficiary IBAN also carries the Debit/Credit amount
        if any(_IBAN_RE.match(t) for t in texts):
            past_iban = True
            debit = credit = 0.0
            for w in row:
                if _AMOUNT_RE.match(w["text"]):
                    mid_x = (w["x0"] + w["x1"]) / 2
                    amt = _parse_amount(w["text"])
                    if abs(mid_x - debit_x) < abs(mid_x - credit_x):
                        debit = amt
                    else:
                        credit = amt
            current["amount"] = credit - debit
            continue

        # Rows after the IBAN line → Назначение платежа (description)
        if past_iban:
            current["description"] = (current["description"] + " " + " ".join(texts)).strip()

    # Flush last transaction
    if current is not None and current["amount"] is not None:
        _finalize(current)
        records.append(current)

    return records


def _finalize(record: dict) -> None:
    desc = record["description"]
    # Extract actual card transaction date
    m = _TX_DATE_RE.search(desc)
    if m:
        record["tx_date"] = _fmt(m.group(1))
    # Use "Операция: ..." as the meaningful description for card purchases
    m = _OPERACIA_RE.search(desc)
    if m:
        record["description"] = m.group(1).strip()
    elif not desc.strip():
        record["description"] = "—"
