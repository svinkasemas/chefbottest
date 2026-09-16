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


def main() -> None:
    print("Проверяю каждого провайдера напрямую (без прокси)...\n")
    for name, call_fn in PROVIDERS:
        try:
            text = call_fn(TEST_PROMPT, False)
            extract_json(text)
            print(f"✅ {name}: работает")
        except Exception as e:
            print(f"❌ {name}: {e}")


if __name__ == "__main__":
    main()
