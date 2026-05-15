import hashlib
import sqlite3
from pathlib import Path
import pandas as pd

DB_PATH = Path(__file__).parent.parent / "transactions.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT    UNIQUE NOT NULL,
                date        TEXT    NOT NULL,
                tx_date     TEXT,
                currency    TEXT    NOT NULL DEFAULT 'EUR',
                amount      REAL    NOT NULL,
                description TEXT,
                category    TEXT,
                bank        TEXT,
                imported_at TEXT    DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_date     ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_category ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_bank     ON transactions(bank);
            """
        )


def _fingerprint(row: dict) -> str:
    key = "|".join(
        [
            str(row.get("bank", "")),
            str(row.get("date", "")),
            str(row.get("tx_date", "")),
            str(row.get("currency", "")),
            f"{float(row.get('amount', 0)):.2f}",
            str(row.get("description", "")),
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()


def insert_transactions(df: pd.DataFrame) -> tuple[int, int]:
    """Insert new transactions, skip duplicates.
    Returns (inserted, skipped).
    """
    init_db()
    inserted = skipped = 0

    with _connect() as conn:
        for _, row in df.iterrows():
            fp = _fingerprint(row.to_dict())
            try:
                conn.execute(
                    """
                    INSERT INTO transactions
                        (fingerprint, date, tx_date, currency, amount, description, category, bank)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fp,
                        row.get("date"),
                        row.get("tx_date"),
                        row.get("currency", "EUR"),
                        float(row.get("amount", 0)),
                        row.get("description"),
                        row.get("category"),
                        row.get("bank"),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1

    return inserted, skipped


def load_transactions(
    bank: str | None = None,
    currency: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> pd.DataFrame:
    init_db()
    query = "SELECT * FROM transactions WHERE 1=1"
    params: list = []
    if bank:
        query += " AND bank = ?"
        params.append(bank)
    if currency:
        query += " AND currency = ?"
        params.append(currency)
    if date_from:
        query += " AND date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND date <= ?"
        params.append(date_to)
    query += " ORDER BY date DESC"

    with _connect() as conn:
        return pd.read_sql_query(query, conn, params=params)


def update_categories(df: pd.DataFrame) -> None:
    """Persist LLM-assigned categories back to the DB by fingerprint."""
    with _connect() as conn:
        for _, row in df.iterrows():
            fp = _fingerprint(row.to_dict())
            conn.execute(
                "UPDATE transactions SET category = ? WHERE fingerprint = ?",
                (row.get("category"), fp),
            )
