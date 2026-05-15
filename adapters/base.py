from abc import ABC, abstractmethod
import pandas as pd


class BankAdapter(ABC):
    bank_name: str = ""

    @classmethod
    @abstractmethod
    def can_handle(cls, first_page_text: str) -> bool:
        """Return True if this adapter recognizes the PDF format."""

    @abstractmethod
    def parse(self, pdf_path: str) -> pd.DataFrame:
        """Parse the PDF and return a DataFrame with the unified schema:
        date, tx_date, currency, amount, description, bank
        amount: negative = expense, positive = income
        """
