"""
Bank 4 adapter — Poštanska Štedionica Serbia.
Columns: DATUM Valuta | DATUM Autor | KARTICA | OPIS | PROMENA | SALDO
Amount format: 10.900,50  (income) or 10.900,50- (expense)
  dot = thousands separator, comma = decimal, minus AFTER digits = negative
Description may span two visual rows.
Currency from header: "u EUR" or "u RSD".
"""
import re
from datetime import datetime
import pdfplumber
import pandas as pd
from .base import BankAdapter

BANK_NAME = "postanska"

_DATE_RE   = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_AMOUNT_RE = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}-?$|^\d+,\d{2}-?$")
_CURRENCY_RE = re.compile(r"\bu\s+(EUR|RSD|USD)\b", re.IGNORECASE)


def _fmt(d: str) -> str:
    return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")


def _parse_amount(s: str) -> float:
    s = s.strip().rstrip(",")
    negative = s.endswith("-")
    s = s.rstrip("-")
    # European format: remove thousands dots, replace comma decimal
    s = s.replace(".", "").replace(",", ".")
    val = float(s)
    return -val if negative else val


def _detect_currency(pdf_path: str) -> str:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = pdf.pages[0].extract_text() or ""
        m = _CURRENCY_RE.search(text)
        if m:
            return m.group(1).upper()
    except Exception:
        pass
    return "RSD"


class Bank4Adapter(BankAdapter):
    bank_name = BANK_NAME

    @classmethod
    def can_handle(cls, first_page_text: str) -> bool:
        t = first_page_text.upper()
        # "POŠTANSKA" covers both Latin (Š→Š) and may appear as ASCII variant
        has_bank = "POŠTANSKA" in t or "ПОШТАНСКА" in t
        has_cols = "PROMENA" in t and "SALDO" in t
        return has_bank or has_cols

    def parse(self, pdf_path: str) -> pd.DataFrame:
        currency = _detect_currency(pdf_path)
        records: list[dict] = []
        promena_x: float | None = None
        opis_x0: float | None = None

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words()
                if not words:
                    continue

                # Locate column centres from headers (once per statement)
                for w in words:
                    if w["text"] == "PROMENA" and promena_x is None:
                        promena_x = (w["x0"] + w["x1"]) / 2
                    if w["text"] == "OPIS" and opis_x0 is None:
                        opis_x0 = w["x0"]

                if promena_x is None:
                    continue

                records.extend(_parse_page(words, promena_x, opis_x0, currency))

        return pd.DataFrame(records)


def _parse_page(
    words: list[dict],
    promena_x: float,
    opis_x0: float | None,
    currency: str,
) -> list[dict]:
    # Bucket words into visual rows by Y (3pt tolerance)
    rows: dict[int, list[dict]] = {}
    for w in words:
        y_key = round(w["top"] / 3) * 3
        rows.setdefault(y_key, []).append(w)
    all_y = sorted(rows.keys())

    # Determine description column start; fall back to a rough x estimate
    desc_x_min = opis_x0 if opis_x0 is not None else promena_x * 0.35
    # Description ends before the PROMENA column (with margin)
    desc_x_max = promena_x - 15

    # Identify key rows: first word is a date
    key_ys: list[int] = []
    for y in all_y:
        row = sorted(rows[y], key=lambda w: w["x0"])
        if row and _DATE_RE.match(row[0]["text"]):
            key_ys.append(y)

    records: list[dict] = []
    for i, ky in enumerate(key_ys):
        row = sorted(rows[ky], key=lambda w: w["x0"])

        date_str = row[0]["text"]

        # Find amount in PROMENA column (closest word to promena_x that matches amount pattern)
        amount: float | None = None
        for w in row:
            mid_x = (w["x0"] + w["x1"]) / 2
            if _AMOUNT_RE.match(w["text"]) and abs(mid_x - promena_x) < 40:
                amount = _parse_amount(w["text"])
                break

        if amount is None:
            continue

        # Collect description words from OPIS column on this row and the next continuation row
        next_ky = key_ys[i + 1] if i + 1 < len(key_ys) else float("inf")
        desc_tokens: list[str] = []

        # Words on the key row itself
        for w in row:
            if desc_x_min <= w["x0"] <= desc_x_max:
                desc_tokens.append(w["text"])

        # Words on intermediate rows (description continuation)
        for y in all_y:
            if y <= ky or y >= next_ky:
                continue
            for w in sorted(rows[y], key=lambda w: w["x0"]):
                if desc_x_min <= w["x0"] <= desc_x_max:
                    desc_tokens.append(w["text"])

        description = " ".join(desc_tokens).strip() or "—"

        records.append({
            "date":        _fmt(date_str),
            "tx_date":     _fmt(date_str),
            "currency":    currency,
            "amount":      amount,
            "description": description,
            "bank":        BANK_NAME,
        })

    return records
