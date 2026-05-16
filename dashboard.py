#!/usr/bin/env python3
import re
from flask import Flask, jsonify, render_template, abort
from core.db import (
    get_months, get_monthly_summary, get_transactions_by_category,
    get_imports, rollback_import,
)

app = Flask(__name__)

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/months")
def api_months():
    return jsonify(get_months())


@app.route("/api/summary/<month>")
def api_summary(month):
    if not _MONTH_RE.match(month):
        abort(400)
    return jsonify(get_monthly_summary(month))


@app.route("/api/transactions/<month>/<path:category>")
def api_transactions(month, category):
    if not _MONTH_RE.match(month):
        abort(400)
    return jsonify(get_transactions_by_category(month, category))


@app.route("/api/imports")
def api_imports():
    return jsonify(get_imports())


@app.route("/api/imports/<import_id>", methods=["DELETE"])
def api_rollback(import_id):
    deleted = rollback_import(import_id)
    return jsonify({"deleted": deleted})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
