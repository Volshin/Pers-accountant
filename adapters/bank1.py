"""
Bank 1 adapter — Alta bank (Serbian), format:
  <num> <DD.MM.YYYY> <DD.MM.YYYY> <description> <-amount> <balance> <DD.MM.YYYY> <ref>
"""
import re
from datetime import datetime
from pathlib import Path
import pdfplumber
import pandas as pd
from .base import BankAdapter

BANK_NAME = "alta"

# ISO 4217 numeric → alpha, for filename detection (e.g. _978_ = EUR)
_ISO_NUMERIC = {
    "978": "EUR", "941": "RSD", "840": "USD",
    "826": "GBP", "756": "CHF", "203": "CZK",
    "348": "HUF", "985": "PLN", "946": "RON",
}
_KNOWN_CURRENCIES = set(_ISO_NUMERIC.values())

_PATTERN = re.compile(
    r"^(\d+)\s+"
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(.+?)\s+"
    r"(-[\d,]+\.\d{2})\s+"      # Odliv / expense
    r"([\d,]+\.\d{2})\s+"       # Stanje / balance
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d+)"
)

_PATTERN_IN = re.compile(
    r"^(\d+)\s+"
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(.+?)\s+"
    r"([\d,]+\.\d{2})\s+"       # Priliv / income (no minus)
    r"([\d,]+\.\d{2})\s+"       # Stanje
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d+)"
)

_EXPENSE_ONLY_RE = re.compile(r"(-[\d,]+\.\d{2})")


def _fmt(d: str) -> str:
    return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")


class Bank1Adapter(BankAdapter):
    bank_name = BANK_NAME

    @classmethod
    def can_handle(cls, first_page_text: str) -> bool:
        return bool(re.search(r"^\d+\s+\d{2}\.\d{2}\.\d{4}", first_page_text, re.MULTILINE))

    def parse(self, pdf_path: str) -> pd.DataFrame:
        currency = self._detect_currency(pdf_path)

        raw_lines = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    raw_lines.extend(text.split("\n"))

        merged = self._merge_lines(raw_lines)
        records = [r for line in merged if (r := self._parse_line(line, currency))]
        return pd.DataFrame(records)

    @staticmethod
    def _detect_currency(pdf_path: str) -> str:
        # 1. Search PDF text for a known 3-letter currency code
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = pdf.pages[0].extract_text() or ""
            # Search only the header (first 15 lines) to avoid false matches in descriptions
            header = "\n".join(text.split("\n")[:15])
            for cur in _KNOWN_CURRENCIES:
                if re.search(rf"\b{cur}\b", header):
                    return cur
        except Exception:
            pass

        # 2. Fall back to ISO numeric code embedded in filename (e.g. _978_)
        m = re.search(r"_(\d{3})_", Path(pdf_path).name)
        if m and m.group(1) in _ISO_NUMERIC:
            return _ISO_NUMERIC[m.group(1)]

        return "EUR"

    @staticmethod
    def _merge_lines(lines: list[str]) -> list[str]:
        merged, current = [], ""
        for line in lines:
            if re.match(r"^\d+\s+\d{2}\.\d{2}\.\d{4}", line.strip()):
                if current:
                    merged.append(current)
                current = line.strip()
            elif current:
                current += " " + line.strip()
        if current:
            merged.append(current)
        return merged

    @staticmethod
    def _parse_line(line: str, currency: str) -> dict | None:
        m = _PATTERN.match(line)
        if m:
            return {
                "date":        _fmt(m.group(2)),
                "tx_date":     _fmt(m.group(7)),
                "currency":    currency,
                "amount":      float(m.group(5).replace(",", "")),
                "description": m.group(4).strip(),
                "bank":        BANK_NAME,
            }
        m2 = _PATTERN_IN.match(line)
        if m2 and not _EXPENSE_ONLY_RE.search(m2.group(5)):
            return {
                "date":        _fmt(m2.group(2)),
                "tx_date":     _fmt(m2.group(7)),
                "currency":    currency,
                "amount":      float(m2.group(5).replace(",", "")),
                "description": m2.group(4).strip(),
                "bank":        BANK_NAME,
            }
        return None
