#!/usr/bin/env python3
"""
Personal Finance Assistant — entry point.

Usage:
    python main.py statement.pdf [statement2.pdf ...]
    python main.py --summary
    python main.py --summary --currency EUR --from 2026-01-01
"""
import argparse
import sys
from uuid import uuid4

import pandas as pd

from core.categorizer import categorize_all
from core.db import insert_transactions, record_import, load_transactions, update_categories
from core.detector import detect_adapter


def import_pdf(path: str) -> None:
    print(f"\n[{path}] Detecting bank format…")
    adapter = detect_adapter(path)
    print(f"[{path}] Adapter: {adapter.bank_name}")

    df = adapter.parse(path)
    if df.empty:
        print(f"[{path}] No transactions parsed.")
        return

    print(f"[{path}] Parsed {len(df)} transactions. Categorizing…")
    tx_types = df["tx_type"].tolist() if "tx_type" in df.columns else None
    df["category"] = categorize_all(df["description"].tolist(), tx_types)

    import_id = str(uuid4())
    inserted, skipped = insert_transactions(df, import_id)
    print(f"[{path}] Saved {inserted} new, skipped {skipped} duplicates.")
    if inserted > 0:
        record_import(import_id, path, adapter.bank_name, inserted)
        print(f"[{path}] Import ID: {import_id}")


def print_summary(
    bank: str | None,
    currency: str | None,
    date_from: str | None,
    date_to: str | None,
) -> None:
    df = load_transactions(bank=bank, currency=currency, date_from=date_from, date_to=date_to)
    if df.empty:
        print("No transactions found.")
        return

    print(f"\n{'='*55}")
    print(f"  РАСХОДЫ ПО КАТЕГОРИЯМ")
    if bank:
        print(f"  Банк: {bank}")
    if date_from or date_to:
        print(f"  Период: {date_from or '…'} — {date_to or '…'}")
    print(f"{'='*55}")

    expenses = df[df["amount"] < 0].copy()
    income = df[df["amount"] > 0].copy()

    for cur, group in expenses.groupby("currency"):
        print(f"\n  Расходы ({cur})")
        print(f"  {'-'*40}")
        summary = (
            group.groupby("category")["amount"]
            .sum()
            .sort_values()
        )
        for cat, total in summary.items():
            print(f"  {cat:<18} {total:>10.2f} {cur}")
        print(f"  {'ИТОГО':<18} {group['amount'].sum():>10.2f} {cur}")

    for cur, group in income.groupby("currency"):
        print(f"\n  Доходы ({cur})")
        print(f"  {'-'*40}")
        summary = group.groupby("category")["amount"].sum().sort_values(ascending=False)
        for cat, total in summary.items():
            print(f"  {cat:<18} {total:>10.2f} {cur}")
        print(f"  {'ИТОГО':<18} {group['amount'].sum():>10.2f} {cur}")

    print(f"\n  Всего транзакций: {len(df)}")
    print(f"{'='*55}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal Finance Assistant")
    parser.add_argument("files", nargs="*", help="PDF statement files to import")
    parser.add_argument("--summary", action="store_true", help="Print spending summary")
    parser.add_argument("--bank", help="Filter summary by bank name")
    parser.add_argument("--currency", help="Filter summary by currency (e.g. EUR)")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    args = parser.parse_args()

    if not args.files and not args.summary:
        parser.print_help()
        sys.exit(1)

    for path in args.files:
        try:
            import_pdf(path)
        except Exception as e:
            print(f"[ERROR] {path}: {e}")

    if args.summary or not args.files:
        print_summary(args.bank, args.currency, args.date_from, args.date_to)


if __name__ == "__main__":
    main()
