import pdfplumber
import pandas as pd
from adapters.base import BankAdapter
from adapters.bank1 import Bank1Adapter
from adapters.bank2 import Bank2Adapter
from adapters.bank3 import Bank3Adapter
from adapters.bank4 import Bank4Adapter

ADAPTERS: list[type[BankAdapter]] = [Bank3Adapter, Bank2Adapter, Bank4Adapter, Bank1Adapter]


def _readable_ratio(text: str) -> float:
    """Fraction of characters that are ordinary letters/digits/punctuation.

    Obfuscated PDFs (e.g. Freedom Bank) render glyphs whose extracted codepoints
    are control bytes mixed with stray latin letters — a very low ratio of
    readable text. If a first page is overwhelmingly garbage, the text path is
    unusable and we should fall back to OCR.
    """
    if not text:
        return 0.0
    total = len(text)
    readable = sum(1 for c in text if c.isalnum() or c in " .,:;\\-()/[]")
    return readable / total


class OcrAdapter(BankAdapter):
    """Adapter that parses via OCR (render + Tesseract) instead of the text layer.

    Used as a fallback when the PDF's embedded text is obfuscated. bank_name is
    'freedom_kz' because that is the only known obfuscated source today.
    """
    bank_name = "freedom_kz"

    @classmethod
    def can_handle(cls, first_page_text: str) -> bool:
        return False  # never selected by text markers; invoked explicitly as fallback.

    def parse(self, pdf_path: str) -> pd.DataFrame:
        from core.ocr import parse_ocr
        return parse_ocr(pdf_path)


def detect_adapter(pdf_path: str) -> BankAdapter:
    with pdfplumber.open(pdf_path) as pdf:
        first_page_text = pdf.pages[0].extract_text() or ""

    # If the text layer is obfuscated (glyphs map to garbage codepoints), go
    # straight to OCR — do NOT try the text adapters, they would only produce
    # junk partial rows. Freedom Bank Kazakhstan is the known case today.
    if _readable_ratio(first_page_text) < 0.5:
        return OcrAdapter()

    for cls in ADAPTERS:
        if cls.can_handle(first_page_text):
            return cls()

    raise ValueError(
        f"No adapter found for '{pdf_path}'. "
        "Add a new adapter in adapters/ and register it in core/detector.py."
    )
