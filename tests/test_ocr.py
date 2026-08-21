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


def test_clean_description_handles_empty():
    assert ocr._clean_description("") == ""


# ── _classify_internal ─────────────────────────────────────────────────────────

def test_classify_internal_conversion():
    assert ocr._classify_internal("23.02.2026 22053 ... Конвертация") == "Конвертация валюты"


def test_classify_internal_deposit_out():
    assert ocr._classify_internal("Выплата вклада с депозитного договора") == "Выплата вклада"


def test_classify_internal_deposit_in():
    assert ocr._classify_internal("Прием вклада по договору") == "Приём вклада"


def test_classify_internal_default():
    assert ocr._classify_internal("что-то другое") == "Внутренний перевод"


# ── _extract_internal_amount ───────────────────────────────────────────────────

def test_extract_internal_amount_from_summe():
    block = "Прием вклада по договору ... сумме 5187.89 EUR"
    assert ocr._extract_internal_amount(block) == 5187.89


def test_extract_internal_amount_fallback_decimal():
    block = "Выплата вклада ... 257.55 KZ..."
    assert ocr._extract_internal_amount(block) == 257.55


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
