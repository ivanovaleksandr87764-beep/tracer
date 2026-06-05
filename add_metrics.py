"""Добавляет SMART-метрики ко всем целям через DeepSeek R1."""
import os, json, httpx, re, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def get_goals():
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/goals", headers=HEADERS,
                  params={"order": "id.asc"})
    return r.json()


def get_tasks_for_goal(goal_id):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                  params={"goal_id": f"eq.{goal_id}"})
    return r.json()


def update_goal(goal_id, data):
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/goals", headers=HEADERS,
                    params={"id": f"eq.{goal_id}"}, json=data)
    return r.json()


def call_deepseek(prompt):
    r = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": "openai/gpt-oss-120b",
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 2000, "temperature": 0.3},
        timeout=90,
    )
    content = r.json()["choices"][0]["message"]["content"].strip()
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
    content = re.sub(r"^```json\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    return json.loads(content)


PROMPT = """Ты стратегический консультант команды онлайн-курса Шамиля (продажи, бизнес-обучение).
Контекст: команда запустила поток на ~17 учеников, следующее окно продаж — конец июня/начало июля.

Цель:
"{title}"
Категория: {category}

Действия которые команда планирует под эту цель:
{tasks}

Твоя задача — переформулировать цель в SMART-формате (Specific, Measurable, Achievable, Relevant, Time-bound).

Верни ТОЛЬКО валидный JSON (без markdown):
{{
  "title": "Переформулированная цель — глагол + результат (макс 80 символов)",
  "metric": "Конкретная измеримая метрика успеха (число + срок). Например: '20 оплативших по 70к до 30 июня' или '3 поста/нед с охватом 1500+ к 15 июня'",
  "deadline": "Реалистичный дедлайн в формате '30 июня' или 'конец июня' или null",
  "reasoning": "1-2 предложения почему именно эта метрика реалистична"
}}

Правила:
- Метрика обязательна и конкретна (с цифрой или фактом)
- Если категория обучение/продукт — метрика про результат учеников
- Если маркетинг/контент — про охват/подписчиков/лидов
- Если продажи — про оплативших/выручку
- Если операционка — про факт внедрения системы (например 'все задачи фиксируются в трекере к 10 июня')"""


def main():
    goals = get_goals()
    print(f"Найдено целей: {len(goals)}\n")

    for g in goals:
        if g.get("metric"):
            print(f"[skip] {g['title']} — метрика уже есть")
            continue

        tasks = get_tasks_for_goal(g["id"])
        tasks_text = "\n".join(f"  - {t['title']}" for t in tasks[:10]) or "  (нет действий)"

        print(f"\n>>> {g['title']}")
        try:
            result = call_deepseek(PROMPT.format(
                title=g["title"], category=g.get("category",""), tasks=tasks_text
            ))
            new_title = result.get("title", g["title"])
            metric = result.get("metric", "")
            deadline = result.get("deadline")
            reasoning = result.get("reasoning", "")

            print(f"    Title:  {new_title}")
            print(f"    Metric: {metric}")
            print(f"    Deadline: {deadline}")
            print(f"    Why: {reasoning}")

            update_goal(g["id"], {
                "title": new_title,
                "metric": metric,
                "deadline": deadline,
            })
            print("    ✓ Обновлено")
        except Exception as e:
            print(f"    ERROR: {e}")

    print("\n✅ Готово!")


if __name__ == "__main__":
    main()
