#!/usr/bin/env bash
# One-shot install/upgrade for Personal Accountant on a Raspberry Pi.
#
# Installs:
#   1. system packages needed by the OCR path (Tesseract + Russian language)
#   2. Python dependencies (pdfplumber, pypdfium2, flask, pandas, watchdog, pytest)
#
# Run from the project root:
#   bash deploy/install.sh
#
# Safe to re-run: apt/pip are idempotent.

set -euo pipefail

echo "==> Installing system packages (Tesseract OCR + Russian)…"
sudo apt-get update
sudo apt-get install -y tesseract-ocr tesseract-ocr-rus

echo "==> Locating project venv…"
if [ -f venv/bin/activate ]; then
    VENV=venv
elif [ -f .venv/bin/activate ]; then
    VENV=.venv
else
    echo "No venv found — creating one at ./venv"
    python3 -m venv venv
    VENV=venv
fi
source "$VENV/bin/activate"

echo "==> Installing Python dependencies…"
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "Done. Next steps:"
echo "  1. cp deploy/.env.example .env   # then edit OLLAMA_HOST and OLLAMA_MODEL"
echo "  2. sudo cp deploy/*.service /etc/systemd/system/"
echo "  3. sudo systemctl daemon-reload && sudo systemctl enable --now finance-dashboard finance-watcher"
