"""
Bank 4 adapter — Poštanska Štedionica Serbia.
Table: DATUM Valuta | DATUM Autor | KARTICA | OPIS | PROMENA | SALDO
Date cells: DD.MM (year extracted from "formiran: DD.MM.YY" in header)
Amount cells: 9,50- or ,43- (dot=thousands, comma=decimal, trailing minus=negative)
Description may span two visual rows.
Currency from header: "u EUR" or "u RSD".
"""
import re
from datetime import datetime
import pdfplumber
import pandas as pd
from .base import BankAdapter

BANK_NAME = "postanska"

_DATE_RE     = re.compile(r"^\d{2}\.\d{2}$")   # DD.MM — no year in table rows
_AMOUNT_RE   = re.compile(r"^\d*,\d{2}-?$|^\d{1,3}(?:\.\d{3})+,\d{2}-?$")
_CURRENCY_RE = re.compile(r"\bu\s+(EUR|RSD|USD)\b", re.IGNORECASE)
# Match "formiran: 05.06.26" or "PERIODU: 29.04.26" — capture 2-digit year
_YEAR_RE     = re.compile(r"(?:formiran:\s*|PERIODU:\s*)\d{2}\.\d{2}\.(\d{2})\b", re.IGNORECASE)


def _fmt(ddmm: str, year: int) -> str:
    return datetime.strptime(f"{ddmm}.{year}", "%d.%m.%Y").strftime("%Y-%m-%d")


def _parse_amount(s: str) -> float:
    s = s.strip().rstrip(",")
    negative = s.endswith("-")
    s = s.rstrip("-")
    s = s.replace(".", "").replace(",", ".") or "0"
    return (-1 if negative else 1) * float(s)


def _detect_currency(text: str) -> str:
    m = _CURRENCY_RE.search(text)
    return m.group(1).upper() if m else "RSD"


def _detect_year(text: str) -> int:
    m = _YEAR_RE.search(text)
    if m:
        return 2000 + int(m.group(1))
    return datetime.now().year


class Bank4Adapter(BankAdapter):
    bank_name = BANK_NAME

    @classmethod
    def can_handle(cls, first_page_text: str) -> bool:
        t = first_page_text.upper()
        return "POŠTANSKA" in t or "ПОШТАНСКА" in t

    def parse(self, pdf_path: str) -> pd.DataFrame:
        records: list[dict] = []
        promena_x: float | None = None
        opis_x0: float | None = None
        currency = "RSD"
        year = datetime.now().year
        table_header_y: float = 0

        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                words = page.extract_words()

                if page_idx == 0:
                    currency = _detect_currency(text)
                    year = _detect_year(text)

                if not words:
                    continue

                for w in words:
                    if w["text"] == "PROMENA" and promena_x is None:
                        promena_x = (w["x0"] + w["x1"]) / 2
                        table_header_y = w["top"]
                    if w["text"] == "OPIS" and opis_x0 is None:
                        opis_x0 = w["x0"]

                if promena_x is None:
                    continue

                records.extend(
                    _parse_page(words, promena_x, opis_x0, table_header_y, currency, year)
                )

        return pd.DataFrame(records)


def _parse_page(
    words: list[dict],
    promena_x: float,
    opis_x0: float | None,
    table_header_y: float,
    currency: str,
    year: int,
) -> list[dict]:
    rows: dict[int, list[dict]] = {}
    for w in words:
        y_key = round(w["top"] / 3) * 3
        rows.setdefault(y_key, []).append(w)
    all_y = sorted(rows.keys())

    desc_x_min = opis_x0 if opis_x0 is not None else promena_x * 0.35
    desc_x_max = promena_x - 15

    # Only look for transaction rows below the table header
    key_ys: list[int] = []
    for y in all_y:
        if y <= table_header_y:
            continue
        row = sorted(rows[y], key=lambda w: w["x0"])
        if row and _DATE_RE.match(row[0]["text"]):
            key_ys.append(y)

    records: list[dict] = []
    for i, ky in enumerate(key_ys):
        row = sorted(rows[ky], key=lambda w: w["x0"])
        ddmm = row[0]["text"]

        amount: float | None = None
        for w in row:
            mid_x = (w["x0"] + w["x1"]) / 2
            if _AMOUNT_RE.match(w["text"]) and abs(mid_x - promena_x) < 40:
                amount = _parse_amount(w["text"])
                break

        if amount is None:
            continue

        next_ky = key_ys[i + 1] if i + 1 < len(key_ys) else float("inf")
        desc_tokens: list[str] = []

        for w in row:
            if desc_x_min <= w["x0"] <= desc_x_max:
                desc_tokens.append(w["text"])

        for y in all_y:
            if y <= ky or y >= next_ky:
                continue
            for w in sorted(rows[y], key=lambda w: w["x0"]):
                if desc_x_min <= w["x0"] <= desc_x_max:
                    desc_tokens.append(w["text"])

        description = " ".join(desc_tokens).strip() or "—"

        try:
            date_str = _fmt(ddmm, year)
        except ValueError:
            continue

        records.append({
            "date":        date_str,
            "tx_date":     date_str,
            "currency":    currency,
            "amount":      amount,
            "description": description,
            "bank":        BANK_NAME,
        })

    return records
