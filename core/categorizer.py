import logging
import os
import re
import json
import requests
from pathlib import Path

log = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL = f"{OLLAMA_HOST.rstrip('/')}/api/generate"
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct-q8_0")

log.info(f"Ollama: {OLLAMA_URL} | model: {MODEL}")

_CATEGORIES_FILE = Path(__file__).parent.parent / "config" / "categories.json"

def _load_categories() -> list[dict]:
    with open(_CATEGORIES_FILE, encoding="utf-8") as f:
        return json.load(f)

BATCH_SIZE = 50


def categorize_all(descriptions: list[str]) -> list[str]:
    results: list[str] = []
    for i in range(0, len(descriptions), BATCH_SIZE):
        batch = descriptions[i : i + BATCH_SIZE]
        results.extend(_categorize_batch(batch))
    return results


def _categorize_batch(descriptions: list[str]) -> list[str]:
    numbered = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(descriptions))
    categories = _load_categories()
    names = ", ".join(c["name"] for c in categories)
    cat_lines = "\n".join(f'- {c["name"]}: {c["hint"]}' for c in categories)
    prompt = (
        f"Определи категорию каждой банковской транзакции по названию торговой точки или описанию платежа.\n"
        f"Названия могут быть на любом языке — определяй по смыслу.\n\n"
        f"Категории:\n{cat_lines}\n\n"
        f"Используй только эти категории: {names}.\n"
        f"Верни ТОЛЬКО JSON массив: [{{\"id\": 1, \"category\": \"...\"}}, ...]\n\n"
        f"Транзакции:\n{numbered}"
    )

    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json()["response"]
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            cats_out = [item.get("category", "Прочее") for item in data]
            if len(cats_out) == len(descriptions):
                return cats_out
    except Exception as e:
        print(f"  [categorizer] error: {e}")

    return ["Прочее"] * len(descriptions)
