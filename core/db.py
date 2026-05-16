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
                import_id   TEXT,
                date        TEXT    NOT NULL,
                tx_date     TEXT,
                currency    TEXT    NOT NULL DEFAULT 'EUR',
                amount      REAL    NOT NULL,
                description TEXT,
                category    TEXT,
                bank        TEXT,
                imported_at TEXT    DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_date      ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_category  ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_bank      ON transactions(bank);

            CREATE TABLE IF NOT EXISTS imports (
                import_id   TEXT PRIMARY KEY,
                filename    TEXT NOT NULL,
                bank        TEXT,
                inserted    INTEGER NOT NULL DEFAULT 0,
                imported_at TEXT    DEFAULT (datetime('now'))
            );
            """
        )
    # Migrate existing databases that don't have import_id yet,
    # then create index (must happen after column exists).
    with _connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
        if "import_id" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN import_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_import_id ON transactions(import_id)")


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


def insert_transactions(df: pd.DataFrame, import_id: str) -> tuple[int, int]:
    """Insert new transactions, skip duplicates. Returns (inserted, skipped)."""
    init_db()
    inserted = skipped = 0

    with _connect() as conn:
        for _, row in df.iterrows():
            fp = _fingerprint(row.to_dict())
            try:
                conn.execute(
                    """
                    INSERT INTO transactions
                        (fingerprint, import_id, date, tx_date, currency, amount, description, category, bank)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fp,
                        import_id,
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


def record_import(import_id: str, filename: str, bank: str, inserted: int) -> None:
    """Write a row to the imports log table."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO imports (import_id, filename, bank, inserted) VALUES (?, ?, ?, ?)",
            (import_id, filename, bank, inserted),
        )


def get_imports() -> list[dict]:
    """Return import history, newest first."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT import_id, filename, bank, inserted, imported_at FROM imports ORDER BY imported_at DESC"
        ).fetchall()
    return [
        {"import_id": r[0], "filename": r[1], "bank": r[2],
         "inserted": r[3], "imported_at": r[4]}
        for r in rows
    ]


def rollback_import(import_id: str) -> int:
    """Delete all transactions for this import_id. Returns count of deleted rows."""
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM transactions WHERE import_id = ?", (import_id,)
        )
        deleted = cur.rowcount
        conn.execute("DELETE FROM imports WHERE import_id = ?", (import_id,))
    return deleted


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


def get_months() -> list[str]:
    """Return distinct YYYY-MM values that have transactions, newest first."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(date, 1, 7) as m FROM transactions ORDER BY m DESC"
        ).fetchall()
    return [r[0] for r in rows]


def get_monthly_summary(month: str) -> dict:
    """Return expense and income totals grouped by category for a given YYYY-MM."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT category, currency, SUM(amount) as total, COUNT(*) as cnt
            FROM transactions
            WHERE substr(date, 1, 7) = ?
            GROUP BY category, currency
            ORDER BY total ASC
            """,
            (month,),
        ).fetchall()

    expenses, income = [], []
    for cat, cur, total, cnt in rows:
        item = {"category": cat or "Прочее", "currency": cur, "total": round(total, 2), "count": cnt}
        (expenses if total < 0 else income).append(item)

    return {"expenses": expenses, "income": income}


def get_transactions_by_category(month: str, category: str) -> list[dict]:
    """Return transactions for a given month and category, newest first."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT date, tx_date, description, amount, currency, bank
            FROM transactions
            WHERE substr(date, 1, 7) = ? AND COALESCE(category, 'Прочее') = ?
            ORDER BY date DESC
            """,
            (month, category),
        ).fetchall()
    return [
        {"date": r[0], "tx_date": r[1], "description": r[2],
         "amount": r[3], "currency": r[4], "bank": r[5]}
        for r in rows
    ]


def update_categories(df: pd.DataFrame) -> None:
    """Persist LLM-assigned categories back to the DB by fingerprint."""
    with _connect() as conn:
        for _, row in df.iterrows():
            fp = _fingerprint(row.to_dict())
            conn.execute(
                "UPDATE transactions SET category = ? WHERE fingerprint = ?",
                (row.get("category"), fp),
            )
