"""
Проверяет каждого ИИ-провайдера по отдельности (не как обычная генерация,
где используется первый успешный из цепочки, а именно каждого) - чтобы
видеть, какие из настроенных ключей реально работают прямо сейчас.

Запуск (из корня проекта, с активированным venv):
    python -m scripts.check_ai_providers
"""
from __future__ import annotations

from backend.ai_recipe import (
    call_cerebras,
    call_gemini,
    call_groq,
    call_nvidia,
    call_openrouter,
    extract_json,
)
from backend.config import PROXY_URL

TEST_PROMPT = (
    'Ответь строго одним JSON-объектом без пояснений: {"ok": true}'
)

PROVIDERS = [
    ("Gemini", call_gemini),
    ("Groq", call_groq),
    ("Cerebras", call_cerebras),
    ("OpenRouter", call_openrouter),
    ("NVIDIA NIM", call_nvidia),
]


def check_one(name: str, call_fn, use_proxy: bool) -> None:
    label = f"{name} ({'через прокси' if use_proxy else 'напрямую'})"
    try:
        text = call_fn(TEST_PROMPT, use_proxy)
        extract_json(text)
        print(f"✅ {label}: работает")
    except Exception as e:
        print(f"❌ {label}: {e}")


def main() -> None:
    print(f"PROXY_URL {'задан' if PROXY_URL else 'НЕ задан'} в .env\n")
    print("Проверяю каждого провайдера напрямую и через прокси...\n")
    for name, call_fn in PROVIDERS:
        check_one(name, call_fn, use_proxy=False)
        if PROXY_URL:
            check_one(name, call_fn, use_proxy=True)
        print()


if __name__ == "__main__":
    main()
