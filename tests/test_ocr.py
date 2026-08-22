"""Unit tests for core OCR parsing helpers and DB normalization.

These test pure functions only — no PDF fixtures, no network, no Tesseract.
Real-statement integration tests belong on the Raspberry Pi (see
test_real_statements.py in deploy/) and are intentionally not run here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import ocr
from core.db import normalize_merchant


# ── normalize_merchant ─────────────────────────────────────────────────────────

def test_normalize_merchant_lowercases_and_collapses():
    assert normalize_merchant("Покупка  с нашей   карты") == "покупка с нашей карты"


def test_normalize_merchant_strips_trailing_punct():
    assert normalize_merchant("Lidl sagt Danke.") == "lidl sagt danke"


def test_normalize_merchant_empty():
    assert normalize_merchant("") == ""
    assert normalize_merchant(None) == ""


# ── _parse_number ──────────────────────────────────────────────────────────────

def test_parse_number_plain_decimal():
    assert ocr._parse_number("54.91") == 54.91


def test_parse_number_comma_decimal():
    assert ocr._parse_number("3,40") == 3.40


def test_parse_number_with_spaces_thousands():
    assert ocr._parse_number("6 487.95") == 6487.95


def test_parse_number_returns_zero_on_garbage():
    assert ocr._parse_number("abc") == 0.0
    assert ocr._parse_number(None) == 0.0


# ── _clean_description ─────────────────────────────────────────────────────────

def test_clean_description_collapses_and_trims_noise():
    desc = ocr._clean_description("Валерьевич   Покупка  с  нашей  карты")
    assert desc == "Покупка с нашей карты"


def test_clean_description_strips_inline_holder_noise():
    desc = ocr._clean_description("Покупка с нашей Казахстан Валерьевич карты")
    assert desc == "Покупка с нашей карты"


def test_clean_description_handles_empty():
    assert ocr._clean_description("") == ""


# ── _header_amount / _fallback_amount ──────────────────────────────────────────

def test_header_amount_from_decimal_before_docid():
    line = "23.02.2026 91820 Банк ... К2655518629508749Е/В 54.91 254462 Номер карты: 5269 2208 Сумма"
    assert ocr._header_amount(line) == 54.91


def test_header_amount_negative_cash_withdrawal():
    line = "25.02.2026 514730 ... KZ65551B629S08749EUR — 250.00 173982 Номер карты: 5269 2208 Сумма"
    assert ocr._header_amount(line) == 250.0


def test_header_amount_none_when_missing():
    line = "24.02.2026 213612 ... KZ65551B629508749EUR i 106888 Номер карты: 5269 2208 Сумма"
    assert ocr._header_amount(line) is None


def test_fallback_amount_from_transactions_decimal():
    block = ". 188116 транзакции: 1188.16 EUR Операция: Покупка с нашей"
    assert ocr._fallback_amount(block) == 1188.16


def test_fallback_amount_ignores_summe_deposit():
    block = "Прием вклада ... сумме 5187.89 EUR. Вкладчик"
    assert ocr._fallback_amount(block) == 5187.89


# ── balance check ──────────────────────────────────────────────────────────────

def test_balance_ok_when_matches():
    res = ocr.OcrResult(
        records=[{"amount": -100.0}, {"amount": 50.0}],
        opening_balance=1000.0,
        closing_balance=950.0,
    )
    ocr._check_balance(res)
    assert res.balance_ok is True
    assert res.balance_delta == 0.0


def test_balance_flags_delta():
    res = ocr.OcrResult(
        records=[{"amount": -100.0}],
        opening_balance=1000.0,
        closing_balance=800.0,
    )
    ocr._check_balance(res)
    assert res.balance_ok is False
    assert res.balance_delta == -100.0
    assert any("Сальдо не сходится" in w for w in res.warnings)
