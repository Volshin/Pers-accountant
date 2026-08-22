import logging
import os
import re
import json
import requests
from pathlib import Path

log = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL = f"{OLLAMA_HOST.rstrip('/')}/api/generate"
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:latest")

log.info(f"Ollama: {OLLAMA_URL} | model: {MODEL}")

_CATEGORIES_FILE = Path(__file__).parent.parent / "config" / "categories.json"

BATCH_SIZE = 50


def _load_categories() -> list[dict]:
    with open(_CATEGORIES_FILE, encoding="utf-8") as f:
        return json.load(f)


INTERNAL_CATEGORY = "Внутренние переводы"

# Deterministic, keyword-based pre-categorization for bank service rows whose
# type is self-evident from the wording even when Freedom leaves no merchant.
# These never hit the LLM, so they can't be mis-filed into "Прочее".
_KEYWORD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Снятие наличных|выдачу в АТМ|выдаче наличных", re.IGNORECASE), "Наличные"),
    (re.compile(r"Комиссия|комисси", re.IGNORECASE), "Комиссии"),
    (re.compile(r"AFT перевод|Списание со счета|перевод", re.IGNORECASE), "Перевод"),
    (re.compile(r"Возврат покупки|возврат средств|refund", re.IGNORECASE), "Возврат"),
]


def _keyword_category(desc: str) -> str | None:
    for rx, cat in _KEYWORD_RULES:
        if rx.search(desc):
            return cat
    return None


def categorize_all(descriptions: list[str], tx_types: list[str] | None = None) -> list[str]:
    """Assign a category to each description.

    tx_types (parallel to descriptions) lets internal/service transactions
    (deposits, conversions) bypass the LLM — they get INTERNAL_CATEGORY directly
    and are never sent to Ollama, since their category is self-evident.
    """
    from core.db import get_merchant_rules, normalize_merchant
    rules = {normalize_merchant(r["merchant"]): r["category"] for r in get_merchant_rules()}

    results: list[str | None] = [None] * len(descriptions)
    llm_indices: list[int] = []

    for i, desc in enumerate(descriptions):
        if tx_types and (tx_types[i] if i < len(tx_types) else None) == "internal":
            results[i] = INTERNAL_CATEGORY
            continue
        # Deterministic service-type detection first (cash, fees, transfers, refunds)
        kw = _keyword_category(desc or "")
        if kw:
            results[i] = kw
            continue
        key = normalize_merchant(desc)
        if key in rules:
            results[i] = rules[key]
        else:
            llm_indices.append(i)

    if llm_indices:
        llm_descs = [descriptions[i] for i in llm_indices]
        llm_cats = _categorize_batches(llm_descs, rules)
        for i, cat in zip(llm_indices, llm_cats):
            results[i] = cat

    return [r or "Прочее" for r in results]


def _categorize_batches(descriptions: list[str], rules: dict[str, str]) -> list[str]:
    results: list[str] = []
    for i in range(0, len(descriptions), BATCH_SIZE):
        batch = descriptions[i : i + BATCH_SIZE]
        results.extend(_categorize_batch(batch, rules))
    return results


def _categorize_batch(descriptions: list[str], rules: dict[str, str]) -> list[str]:
    numbered = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(descriptions))
    categories = _load_categories()
    names = ", ".join(c["name"] for c in categories)
    cat_lines = "\n".join(f'- {c["name"]}: {c["hint"]}' for c in categories)

    examples_block = ""
    if rules:
        sample = list(rules.items())[:10]
        examples_block = "Известные правила мерчантов:\n" + "\n".join(
            f'- "{m}" → {c}' for m, c in sample
        ) + "\n\n"

    prompt = (
        f"Определи категорию каждой банковской транзакции по названию торговой точки или описанию платежа.\n"
        f"Названия могут быть на любом языке — определяй по смыслу.\n\n"
        f"{examples_block}"
        f"Категории:\n{cat_lines}\n\n"
        f"Используй только эти категории: {names}.\n"
        f"Верни ТОЛЬКО JSON массив: [{{\"id\": 1, \"category\": \"...\"}}, ...]\n\n"
        f"Транзакции:\n{numbered}"
    )

    valid = {c["name"] for c in categories}

    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json()["response"]

        # The model frequently wraps the array in ``` fences, adds prose, or
        # renames keys ("name"/"label" instead of "category"). Extract the first
        # JSON array, then recover the category field per item with a tolerant
        # scan — never let a single malformed chunk nuke the whole batch into
        # "Прочее".
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            cats_out: list[str] = []
            for item in data:
                if not isinstance(item, dict):
                    cats_out.append("Прочее")
                    continue
                candidate = (
                    item.get("category")
                    or item.get("категория")
                    or item.get("name")
                    or item.get("label")
                    or ""
                )
                candidate = str(candidate).strip()
                # Map to the closest known category (case/first-word tolerant)
                resolved = None
                for cname in valid:
                    if candidate == cname or candidate.lower() == cname.lower():
                        resolved = cname
                        break
                if resolved is None:
                    for cname in valid:
                        if cname.lower() in candidate.lower():
                            resolved = cname
                            break
                cats_out.append(resolved or "Прочее")
            if len(cats_out) == len(descriptions):
                return cats_out

        # Fallback: even without brackets, look for known category names in the
        # raw reply, in order, and assign greedily if the count matches.
        found = [c for c in valid if c in raw]
        if len(found) == len(descriptions):
            return found
    except Exception as e:
        print(f"  [categorizer] error: {e}")

    return ["Прочее"] * len(descriptions)
