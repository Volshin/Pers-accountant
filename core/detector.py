import pdfplumber
from adapters.base import BankAdapter
from adapters.bank1 import Bank1Adapter
from adapters.bank2 import Bank2Adapter
from adapters.bank3 import Bank3Adapter
from adapters.bank4 import Bank4Adapter

ADAPTERS: list[type[BankAdapter]] = [Bank3Adapter, Bank2Adapter, Bank4Adapter, Bank1Adapter]


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
