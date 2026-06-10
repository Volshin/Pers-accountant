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

            CREATE TABLE IF NOT EXISTS exchange_rates (
                month       TEXT NOT NULL,
                currency    TEXT NOT NULL,
                rate_to_eur REAL NOT NULL,
                updated_at  TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (month, currency)
            );

            CREATE TABLE IF NOT EXISTS merchant_rules (
                merchant    TEXT PRIMARY KEY,
                category    TEXT NOT NULL,
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            """
        )
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
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO imports (import_id, filename, bank, inserted) VALUES (?, ?, ?, ?)",
            (import_id, filename, bank, inserted),
        )


def get_imports() -> list[dict]:
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
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(date, 1, 7) as m FROM transactions ORDER BY m DESC"
        ).fetchall()
    return [r[0] for r in rows]


def get_currencies() -> list[str]:
    """Return distinct currencies present in transactions."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT currency FROM transactions ORDER BY currency"
        ).fetchall()
    return [r[0] for r in rows]


# ── Exchange rates ─────────────────────────────────────────────────────────────

def get_exchange_rates(month: str) -> dict[str, float]:
    """Return most recent {currency: rate_to_eur} available on or before month."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT e1.currency, e1.rate_to_eur
            FROM exchange_rates e1
            WHERE e1.month = (
                SELECT MAX(e2.month) FROM exchange_rates e2
                WHERE e2.currency = e1.currency AND e2.month <= ?
            )
            """,
            (month,),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_all_exchange_rates() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT month, currency, rate_to_eur, updated_at FROM exchange_rates ORDER BY month DESC, currency"
        ).fetchall()
    return [{"month": r[0], "currency": r[1], "rate_to_eur": r[2], "updated_at": r[3]} for r in rows]


def set_exchange_rate(month: str, currency: str, rate: float) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO exchange_rates (month, currency, rate_to_eur, updated_at) VALUES (?, ?, ?, datetime('now'))",
            (month, currency.upper(), rate),
        )


def delete_exchange_rate(month: str, currency: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM exchange_rates WHERE month = ? AND currency = ?",
            (month, currency.upper()),
        )


# ── Merchant rules ─────────────────────────────────────────────────────────────

def get_merchant_rules() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT merchant, category, updated_at FROM merchant_rules ORDER BY merchant"
        ).fetchall()
    return [{"merchant": r[0], "category": r[1], "updated_at": r[2]} for r in rows]


def set_merchant_rule(merchant: str, category: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO merchant_rules (merchant, category, updated_at) VALUES (?, ?, datetime('now'))",
            (merchant, category),
        )


def delete_merchant_rule(merchant: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM merchant_rules WHERE merchant = ?", (merchant,))


def apply_merchant_rule_to_existing(merchant: str, category: str) -> int:
    """Update category for all existing transactions with this description. Returns count."""
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE transactions SET category = ? WHERE description = ?",
            (category, merchant),
        )
    return cur.rowcount


# ── Transaction category update ────────────────────────────────────────────────

def update_transaction_category(tx_id: int, category: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE transactions SET category = ? WHERE id = ?", (category, tx_id)
        )


# ── Summary ────────────────────────────────────────────────────────────────────

def _convert_to_base(
    amount: float, from_cur: str, base_cur: str, rates: dict[str, float]
) -> tuple[float, bool]:
    """Convert amount from from_cur to base_cur. Returns (amount, converted_ok)."""
    if from_cur == base_cur:
        return amount, True

    # to EUR first
    if from_cur == "EUR":
        eur = amount
    elif from_cur in rates:
        eur = amount * rates[from_cur]
    else:
        return amount, False

    if base_cur == "EUR":
        return eur, True
    elif base_cur in rates:
        return eur / rates[base_cur], True
    else:
        return amount, False


def get_monthly_summary(month: str, base_currency: str = "EUR") -> dict:
    init_db()
    base_currency = base_currency.upper()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(category, 'Прочее'), currency, SUM(amount), COUNT(*)
            FROM transactions
            WHERE substr(date, 1, 7) = ?
            GROUP BY COALESCE(category, 'Прочее'), currency
            ORDER BY COALESCE(category, 'Прочее'), currency
            """,
            (month,),
        ).fetchall()

    rates = get_exchange_rates(month)

    by_cat: dict[str, dict] = {}
    for cat, cur, total, cnt in rows:
        if cat not in by_cat:
            by_cat[cat] = {"total_base": 0.0, "count": 0, "breakdown": [], "missing_rate": False}
        converted, ok = _convert_to_base(total, cur, base_currency, rates)
        if not ok:
            by_cat[cat]["missing_rate"] = True
        by_cat[cat]["total_base"] += converted
        by_cat[cat]["count"] += cnt
        by_cat[cat]["breakdown"].append({"currency": cur, "total": round(total, 2), "count": cnt})

    expenses, income = [], []
    for cat, data in by_cat.items():
        item = {
            "category": cat,
            "total": round(data["total_base"], 2),
            "currency": base_currency,
            "count": data["count"],
            "missing_rate": data["missing_rate"],
            "breakdown": data["breakdown"],
        }
        (expenses if data["total_base"] < 0 else income).append(item)

    expenses.sort(key=lambda x: x["total"])
    income.sort(key=lambda x: -x["total"])

    return {"expenses": expenses, "income": income, "base_currency": base_currency}


def get_transactions_by_category(month: str, category: str) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, date, tx_date, description, amount, currency, bank, category
            FROM transactions
            WHERE substr(date, 1, 7) = ? AND COALESCE(category, 'Прочее') = ?
            ORDER BY date DESC
            """,
            (month, category),
        ).fetchall()
    return [
        {"id": r[0], "date": r[1], "tx_date": r[2], "description": r[3],
         "amount": r[4], "currency": r[5], "bank": r[6], "category": r[7]}
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
