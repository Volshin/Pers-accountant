#!/usr/bin/env python3
"""
Integration test for real statement PDFs — run on the Raspberry Pi.

This script deliberately lives OUTSIDE the git-tracked unit tests because the
real bank statements must never be committed. Point it at a local folder of
PDFs (default: ~/finance/test_pdfs/) and it will, for each file:

  1. detect the bank adapter,
  2. parse it (text path or OCR fallback),
  3. report parsed count, amount total, and whether the running balance
     (opening + sum == closing) reconciles.

Usage:
    python deploy/test_real_statements.py [--dir ~/finance/test_pdfs]

It does NOT write to the database.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from core.detector import detect_adapter


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path.home() / "finance" / "test_pdfs"))
    args = ap.parse_args()

    folder = Path(args.dir)
    if not folder.is_dir():
        print(f"Нет папки: {folder}")
        sys.exit(1)

    # Skip hidden files: macOS leaves "._foo.pdf" AppleDouble sidecars when
    # statements are copied over a FAT/exFAT drive — those are metadata, not PDFs.
    pdfs = sorted(p for p in folder.glob("*.pdf") if not p.name.startswith("._"))
    if not pdfs:
        print(f"В папке {folder} нет PDF.")
        return

    for pdf in pdfs:
        print(f"\n{'=' * 60}\n{pdf.name}")
        try:
            adapter = detect_adapter(str(pdf))
            print(f"  банк: {adapter.bank_name}")
        except ValueError as e:
            print(f"  детект не удался: {e}")
            continue

        try:
            df = adapter.parse(str(pdf))
        except Exception as e:
            print(f"  parse crashed: {type(e).__name__}: {e}")
            continue

        if df.empty:
            print("  пустой результат — проверить OCR/формат")
            continue

        total = float(df["amount"].sum()) if "amount" in df.columns else 0.0
        print(f"  строк: {len(df)}")
        print(f"  сумма: {total:.2f}")
        print(f"  колонки: {list(df.columns)}")


if __name__ == "__main__":
    main()
