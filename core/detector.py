import pdfplumber
from adapters.base import BankAdapter
from adapters.bank1 import Bank1Adapter
from adapters.bank2 import Bank2Adapter

ADAPTERS: list[type[BankAdapter]] = [Bank1Adapter, Bank2Adapter]


def detect_adapter(pdf_path: str) -> BankAdapter:
    with pdfplumber.open(pdf_path) as pdf:
        first_page_text = pdf.pages[0].extract_text() or ""

    for cls in ADAPTERS:
        if cls.can_handle(first_page_text):
            return cls()

    raise ValueError(
        f"No adapter found for '{pdf_path}'. "
        "Add a new adapter in adapters/ and register it in core/detector.py."
    )
