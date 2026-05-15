"""
Bank 2 adapter — slash-date format with separate Credit/Debit columns:
  Value date | Transaction date | Currency code | Credit | Debit | Description
  DD/MM/YYYY   DD/MM/YYYY         EUR              [amt]   [amt]   <text>
"""
import re
import pdfplumber
import pandas as pd
from .base import BankAdapter

BANK_NAME = "bank2"
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")


class Bank2Adapter(BankAdapter):
    bank_name = BANK_NAME

    @classmethod
    def can_handle(cls, first_page_text: str) -> bool:
        return bool(re.search(r"Credit\s+Debit", first_page_text)) and bool(
            re.search(r"\d{2}/\d{2}/\d{4}", first_page_text)
        )

    def parse(self, pdf_path: str) -> pd.DataFrame:
        records = []
        credit_x: float | None = None
        debit_x: float | None = None

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words()
                if not words:
                    continue

                # Locate Credit/Debit column midpoints from header (once per statement)
                for w in words:
                    if w["text"] == "Credit" and credit_x is None:
                        credit_x = (w["x0"] + w["x1"]) / 2
                    elif w["text"] == "Debit" and debit_x is None:
                        debit_x = (w["x0"] + w["x1"]) / 2

                if credit_x is None or debit_x is None:
                    # Header not found on this page, use text fallback
                    records.extend(self._parse_text_fallback(page))
                    continue

                records.extend(self._parse_page_by_coords(words, credit_x, debit_x))

        return pd.DataFrame(records)

    @staticmethod
    def _parse_page_by_coords(
        words: list[dict], credit_x: float, debit_x: float
    ) -> list[dict]:
        # Bucket words into rows by vertical position (3pt tolerance)
        rows: dict[int, list[dict]] = {}
        for w in words:
            y_key = round(w["top"] / 3) * 3
            rows.setdefault(y_key, []).append(w)

        records = []
        for y_key in sorted(rows):
            row = sorted(rows[y_key], key=lambda w: w["x0"])
            texts = [w["text"] for w in row]

            if len(texts) < 4:
                continue
            if not (_DATE_RE.match(texts[0]) and _DATE_RE.match(texts[1])):
                continue
            if len(texts[2]) != 3 or not texts[2].isalpha():
                continue  # not a currency code

            value_date = texts[0]
            tx_date = texts[1]
            currency = texts[2]

            credit = 0.0
            debit = 0.0
            desc_words: list[str] = []

            for w in row[3:]:
                mid_x = (w["x0"] + w["x1"]) / 2
                if _AMOUNT_RE.match(w["text"]) and not desc_words:
                    amount = float(w["text"].replace(",", ""))
                    if abs(mid_x - credit_x) < abs(mid_x - debit_x):
                        credit = amount
                    else:
                        debit = amount
                else:
                    desc_words.append(w["text"])

            records.append(
                {
                    "date":        value_date,
                    "tx_date":     tx_date,
                    "currency":    currency,
                    "amount":      credit - debit,   # positive = income, negative = expense
                    "description": " ".join(desc_words),
                    "bank":        BANK_NAME,
                }
            )
        return records

    @staticmethod
    def _parse_text_fallback(page) -> list[dict]:
        """Fallback for pages without the header: assume all amounts are Debit."""
        records = []
        text = page.extract_text() or ""
        pattern = re.compile(
            r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+([A-Z]{3})\s+([\d,.]+)\s+(.+)"
        )
        for line in text.split("\n"):
            m = pattern.match(line.strip())
            if m:
                records.append(
                    {
                        "date":        m.group(1),
                        "tx_date":     m.group(2),
                        "currency":    m.group(3),
                        "amount":      -float(m.group(4).replace(",", "")),
                        "description": m.group(5).strip(),
                        "bank":        BANK_NAME,
                    }
                )
        return records
