# Pers-accountant

Personal finance assistant that parses bank statements from PDF, categorizes transactions via a local LLM (Ollama), and stores everything in a local SQLite database.

## What it does

- Parses PDF bank statements from multiple banks with auto-detection of the format
- Categorizes transactions in Russian via Ollama (runs locally, no data leaves your network)
- Stores transactions in SQLite with fingerprint-based deduplication — re-importing the same statement is safe
- Outputs a spending summary grouped by category and currency
- Runs on Raspberry Pi, accessible from any device via Tailscale + Samba share

## Architecture

```
adapters/
  base.py        — abstract adapter interface
  bank1.py       — format: row_num DD.MM.YYYY description -amount (RSD)
  bank2.py       — format: DD/MM/YYYY Credit/Debit EUR columns
core/
  detector.py    — auto-detects bank format from first PDF page
  categorizer.py — batched LLM categorization via Ollama API
  db.py          — SQLite storage + SHA-256 deduplication
main.py          — CLI: import PDFs and print summaries
watcher.py       — watchdog service: monitors inbox/, processes new PDFs automatically
deploy/
  finance-watcher.service  — systemd unit for Raspberry Pi
  smb.conf.snippet         — Samba share config
```

## Usage

```bash
pip install -r requirements.txt

# Import one or more statements
python main.py statement.pdf

# Print summary (all banks, all time)
python main.py --summary

# Filter
python main.py --summary --currency EUR --from 2026-01-01 --bank bank2
```

## Deployment on Raspberry Pi

```bash
git clone https://github.com/Volshin/Pers-accountant.git ~/PersAccountant
cd ~/PersAccountant
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Run the watchdog service
sudo cp deploy/finance-watcher.service /etc/systemd/system/
sudo systemctl enable --now finance-watcher
```

Samba share config is in `deploy/smb.conf.snippet`. Once set up, the `finance/inbox/` folder mounts natively in Finder (macOS) and the Files app (iOS) over Tailscale.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama instance URL |
| `OLLAMA_MODEL` | `llama3.1:8b-instruct-q8_0` | Model to use |
| `FINANCE_DIR` | `~/finance` | Root folder for inbox/reports/done |

## Stack

Python · pdfplumber · pandas · SQLite · Ollama · watchdog · Samba · Tailscale
