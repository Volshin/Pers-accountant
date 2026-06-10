#!/usr/bin/env python3
import re
from flask import Flask, jsonify, render_template, abort, request
from core.db import (
    get_months, get_monthly_summary, get_transactions_by_category,
    get_imports, rollback_import,
    get_all_exchange_rates, get_exchange_rates, set_exchange_rate, delete_exchange_rate,
    get_merchant_rules, set_merchant_rule, delete_merchant_rule, apply_merchant_rule_to_existing,
    update_transaction_category, get_currencies,
)
from pathlib import Path
import json

app = Flask(__name__)

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_CATEGORIES_FILE = Path(__file__).parent / "config" / "categories.json"


def _load_categories() -> list[dict]:
    with open(_CATEGORIES_FILE, encoding="utf-8") as f:
        return json.load(f)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/rates")
def rates_page():
    return render_template("rates.html")


@app.route("/api/months")
def api_months():
    return jsonify(get_months())


@app.route("/api/currencies")
def api_currencies():
    return jsonify(get_currencies())


@app.route("/api/categories")
def api_categories():
    return jsonify([c["name"] for c in _load_categories()])


@app.route("/api/categories", methods=["POST"])
def api_categories_add():
    data = request.get_json(force=True) or {}
    name = data.get("name", "").strip()
    hint = data.get("hint", "").strip()
    if not name:
        abort(400)
    cats = _load_categories()
    if any(c["name"] == name for c in cats):
        return jsonify({"ok": True, "created": False})
    cats.append({"name": name, "hint": hint or name.lower()})
    with open(_CATEGORIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cats, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "created": True})


@app.route("/api/summary/<month>")
def api_summary(month):
    if not _MONTH_RE.match(month):
        abort(400)
    base = request.args.get("base", "EUR").upper()
    return jsonify(get_monthly_summary(month, base_currency=base))


@app.route("/api/transactions/<month>/<path:category>")
def api_transactions(month, category):
    if not _MONTH_RE.match(month):
        abort(400)
    return jsonify(get_transactions_by_category(month, category))


@app.route("/api/transaction/<int:tx_id>/category", methods=["PATCH"])
def api_update_tx_category(tx_id):
    data = request.get_json(force=True)
    category = (data or {}).get("category")
    if not category:
        abort(400)
    update_transaction_category(tx_id, category)
    return jsonify({"ok": True})


@app.route("/api/imports")
def api_imports():
    return jsonify(get_imports())


@app.route("/api/imports/<import_id>", methods=["DELETE"])
def api_rollback(import_id):
    deleted = rollback_import(import_id)
    return jsonify({"deleted": deleted})


# ── Exchange rates ─────────────────────────────────────────────────────────────

@app.route("/api/rates")
def api_rates_all():
    return jsonify(get_all_exchange_rates())


@app.route("/api/rates/<month>")
def api_rates_month(month):
    if not _MONTH_RE.match(month):
        abort(400)
    rates = get_exchange_rates(month)
    return jsonify([{"month": month, "currency": c, "rate_to_eur": r} for c, r in rates.items()])


@app.route("/api/rates/<month>", methods=["POST"])
def api_rates_set(month):
    if not _MONTH_RE.match(month):
        abort(400)
    data = request.get_json(force=True) or {}
    currency = data.get("currency", "").upper()
    rate = data.get("rate")
    if not currency or rate is None:
        abort(400)
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        abort(400)
    if rate <= 0:
        abort(400)
    set_exchange_rate(month, currency, rate)
    return jsonify({"ok": True})


@app.route("/api/rates/<month>/<currency>", methods=["DELETE"])
def api_rates_delete(month, currency):
    if not _MONTH_RE.match(month):
        abort(400)
    delete_exchange_rate(month, currency.upper())
    return jsonify({"ok": True})


# ── Merchant rules ─────────────────────────────────────────────────────────────

@app.route("/api/merchant-rules")
def api_merchant_rules():
    return jsonify(get_merchant_rules())


@app.route("/api/merchant-rules", methods=["POST"])
def api_merchant_rules_set():
    data = request.get_json(force=True) or {}
    merchant = data.get("merchant", "").strip()
    category = data.get("category", "").strip()
    if not merchant or not category:
        abort(400)
    set_merchant_rule(merchant, category)
    updated = apply_merchant_rule_to_existing(merchant, category)
    return jsonify({"ok": True, "updated": updated})


@app.route("/api/merchant-rules/<path:merchant>", methods=["DELETE"])
def api_merchant_rules_delete(merchant):
    delete_merchant_rule(merchant)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
