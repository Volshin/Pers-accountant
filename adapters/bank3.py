"""
Bank 3 adapter — Freedom Bank Kazakhstan (KZ), current account statement.
Table: Дата операции | Номер документа | ... | Дебет | Кредит | Назначение платежа

Key quirk: in pdfplumber text extraction the date and IBAN sit on the SAME visual row,
while description text is spread across rows above AND below that key row.
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
_OPERACIA_RE = re.compile(r"Операция:\s*(.+?)(?=\s+Дата транзакции:|\s+Код авторизации:|$)")
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

                # Locate column centres from the header (once per statement)
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
    # Bucket words into visual rows by Y (3pt tolerance)
    rows: dict[int, list[dict]] = {}
    for w in words:
        y_key = round(w["top"] / 3) * 3
        rows.setdefault(y_key, []).append(w)
    all_y = sorted(rows.keys())

    # Description column starts just right of the Кредит column
    desc_x_min = credit_x + 25

    # Find the table header row so description collection doesn't bleed into page header
    header_y = 0
    for y in all_y:
        if any(w["text"] in ("Дебет", "Кредит") for w in rows[y]):
            header_y = max(header_y, y)

    # ── Pass 1: find key rows (date + IBAN on same visual line) and read amounts ──
    key_ys: list[int] = []
    key_data: dict[int, dict] = {}

    for y in all_y:
        row = sorted(rows[y], key=lambda w: w["x0"])
        texts = [w["text"] for w in row]
        if not (_DATE_RE.match(texts[0]) and any(_IBAN_RE.match(t) for t in texts)):
            continue

        key_ys.append(y)
        debit = credit = 0.0
        debit_pref = credit_pref = ""

        # Scan key row AND adjacent rows (±1 bucket / ±3pt): amounts often appear
        # just above or below the date/IBAN row due to PDF column wrapping.
        for y2 in all_y:
            if abs(y2 - y) > 3:
                continue
            row2 = sorted(rows[y2], key=lambda w: w["x0"])
            for idx, w in enumerate(row2):
                t = w["text"]
                mid_x = (w["x0"] + w["x1"]) / 2
                if mid_x >= desc_x_min:
                    continue
                if _AMOUNT_RE.match(t):
                    # Skip description currency annotations e.g. "1188.16 EUR Операция:"
                    nxt = row2[idx + 1] if idx + 1 < len(row2) else None
                    if nxt and nxt["text"] == currency and nxt["x0"] < w["x1"] + 30:
                        continue
                    amt = _parse_amount(t)
                    if abs(mid_x - debit_x) <= abs(mid_x - credit_x):
                        if debit == 0:
                            debit = amt
                    else:
                        if credit == 0:
                            credit = amt
                elif re.match(r"^\d{1,5}$", t):
                    if abs(mid_x - debit_x) < 25:
                        debit_pref = t
                    elif abs(mid_x - credit_x) < 25:
                        credit_pref = t

        key_data[y] = {
            "date": texts[0], "debit": debit, "credit": credit,
            "debit_pref": debit_pref, "credit_pref": credit_pref,
        }

    # ── Pass 2: resolve amounts split across two rows (e.g. "5" + "187.89" → 5187.89) ──
    for i, ky in enumerate(key_ys):
        kd = key_data[ky]
        if not kd["debit_pref"] and not kd["credit_pref"]:
            continue
        next_ky = key_ys[i + 1] if i + 1 < len(key_ys) else float("inf")
        for y in (y for y in all_y if ky < y < next_ky):
            for w in sorted(rows[y], key=lambda w: w["x0"]):
                t = w["text"]
                mid_x = (w["x0"] + w["x1"]) / 2
                if mid_x >= desc_x_min or not _AMOUNT_RE.match(t):
                    continue
                if kd["debit_pref"] and abs(mid_x - debit_x) < 25:
                    kd["debit"] = _parse_amount(kd["debit_pref"] + t)
                    kd["debit_pref"] = ""
                elif kd["credit_pref"] and abs(mid_x - credit_x) < 25:
                    kd["credit"] = _parse_amount(kd["credit_pref"] + t)
                    kd["credit_pref"] = ""
            break  # only the immediate next row can carry the continuation

    # ── Pass 3: collect descriptions and build records ──
    records: list[dict] = []

    for i, ky in enumerate(key_ys):
        kd = key_data[ky]
        prev_y = key_ys[i - 1] if i > 0 else header_y
        next_y = key_ys[i + 1] if i + 1 < len(key_ys) else float("inf")

        # Gather words from the description column within this transaction's Y range
        desc_tokens: list[str] = []
        for y in all_y:
            if y <= prev_y or y >= next_y:
                continue
            for w in sorted(rows[y], key=lambda w: w["x0"]):
                if w["x0"] >= desc_x_min:
                    desc_tokens.append(w["text"])

        desc_raw = " ".join(desc_tokens).strip()

        m = _TX_DATE_RE.search(desc_raw)
        tx_date_str = m.group(1) if m else kd["date"]

        m = _OPERACIA_RE.search(desc_raw)
        if m:
            description = m.group(1).strip()
        else:
            fallback = re.split(r"\s+Дата транзакции:", desc_raw)[0].strip()
            description = fallback[:200] or "—"

        records.append({
            "date":        _fmt(kd["date"]),
            "tx_date":     _fmt(tx_date_str),
            "currency":    currency,
            "amount":      round(kd["credit"] - kd["debit"], 2),
            "description": description,
            "bank":        BANK_NAME,
        })

    return records
