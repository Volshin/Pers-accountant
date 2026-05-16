"""
Bank 1 adapter — Alta bank (Serbian), format:
  <num> <DD.MM.YYYY> <DD.MM.YYYY> <description> <-amount> <balance> <DD.MM.YYYY> <ref>
"""
import re
from datetime import datetime
import pdfplumber
import pandas as pd
from .base import BankAdapter

BANK_NAME = "alta"
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")

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


class Bank1Adapter(BankAdapter):
    bank_name = BANK_NAME

    @classmethod
    def can_handle(cls, first_page_text: str) -> bool:
        # Distinctive: line starting with digits + dot-formatted date
        return bool(re.search(r"^\d+\s+\d{2}\.\d{2}\.\d{4}", first_page_text, re.MULTILINE))

    def parse(self, pdf_path: str) -> pd.DataFrame:
        raw_lines = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    raw_lines.extend(text.split("\n"))

        merged = self._merge_lines(raw_lines)
        records = []
        for line in merged:
            rec = self._parse_line(line)
            if rec:
                records.append(rec)

        return pd.DataFrame(records)

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
    def _fmt(d: str) -> str:
        return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")

    def _parse_line(self, line: str) -> dict | None:
        m = _PATTERN.match(line)
        if m:
            return {
                "date":        self._fmt(m.group(2)),
                "tx_date":     self._fmt(m.group(7)),
                "currency":    "RSD",
                "amount":      float(m.group(5).replace(",", "")),
                "description": m.group(4).strip(),
                "bank":        BANK_NAME,
            }
        m2 = _PATTERN_IN.match(line)
        if m2 and not _EXPENSE_ONLY_RE.search(m2.group(5)):
            return {
                "date":        self._fmt(m2.group(2)),
                "tx_date":     self._fmt(m2.group(7)),
                "currency":    "RSD",
                "amount":      float(m2.group(5).replace(",", "")),
                "description": m2.group(4).strip(),
                "bank":        BANK_NAME,
            }
        return None
