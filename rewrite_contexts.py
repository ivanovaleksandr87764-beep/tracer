"""Переписывает контексты всех задач: убирает мусор, делает связь с метрикой цели."""
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


def call_ai(prompt):
    r = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": "openai/gpt-oss-120b",
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 1500, "temperature": 0.3},
        timeout=60,
    )
    content = r.json()["choices"][0]["message"]["content"].strip()
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
    content = re.sub(r"^```json\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    return json.loads(content)


PROMPT = """Ты переписываешь "зачем" под задачей. Текущий контекст содержит мусор (репорты зумов, имена, дату), а должен отвечать на вопрос: КАК эта задача движет к метрике цели.

ЦЕЛЬ: {goal_title}
МЕТРИКА: {metric}

Задача: {task_title}
Старый контекст: {old_context}

Перепиши "зачем" в одно короткое предложение (макс 90 символов).
- Без префиксов "Репорт зума", "Шамиль сказал", "Тимур попросил"
- Связь с метрикой цели или конечным результатом
- Без воды и общих фраз

Верни ТОЛЬКО JSON (без markdown):
{{"why": "новый контекст (одно предложение)"}}"""


def main():
    # Получаем все задачи и цели
    tasks = httpx.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                      params={"order": "id.asc"}).json()
    goals = httpx.get(f"{SUPABASE_URL}/rest/v1/goals", headers=HEADERS).json()
    goals_map = {g["id"]: g for g in goals}

    print(f"Задач: {len(tasks)}\n")

    import time
    skip_markers = ["Репорт зума", "Тимур:", "Шамиль:", "Лена:", "Тимур прописал",
                    "Шамиль прошёл", "Шамиль осознал", "По Мезирову", "SHIFT:",
                    "После запуска", "Пост с результатами", "Ученики присылают",
                    "Ещё один эфир", "Тимур попросил", "Не указано", "Сертификат",
                    "Ключевое отличие"]

    for t in tasks:
        old = t.get("context", "")
        if not old or len(old) < 5:
            continue
        # Если контекст уже короткий и без мусора — пропускаем
        if len(old) < 100 and not any(m in old for m in skip_markers):
            print(f"[skip] [{t['id']}] контекст уже чистый")
            continue
        time.sleep(2)  # rate limit

        gid = t.get("goal_id")
        goal = goals_map.get(gid, {})
        goal_title = goal.get("title", "(без цели)")
        metric = goal.get("metric", "—")

        print(f"\n>>> [{t['id']}] {t['title'][:60]}")
        print(f"    Старый: {old[:100]}...")

        try:
            result = call_ai(PROMPT.format(
                goal_title=goal_title,
                metric=metric,
                task_title=t["title"],
                old_context=old[:500],
            ))
            new_why = result.get("why", "").strip()
            if not new_why or len(new_why) < 5:
                print("    skip (пустой ответ)")
                continue

            print(f"    Новый: {new_why}")

            httpx.patch(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                        params={"id": f"eq.{t['id']}"},
                        json={"context": new_why})
            print("    ✓ обновлено")
        except Exception as e:
            print(f"    ERROR: {e}")

    print("\n✅ Готово!")


if __name__ == "__main__":
    main()
