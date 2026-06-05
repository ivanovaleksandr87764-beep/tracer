"""One-time script: group existing tasks into goals using Groq."""
import os, json, httpx, sys, io
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


def get_tasks():
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                  params={"order": "id.asc"})
    return r.json()


def create_goal(goal: dict) -> int:
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/goals", headers=HEADERS, json=goal)
    return r.json()[0]["id"]


def link_task_to_goal(task_id: int, goal_id: int):
    httpx.patch(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                params={"id": f"eq.{task_id}"}, json={"goal_id": goal_id})


def group_with_groq(tasks: list) -> dict:
    tasks_text = "\n".join(
        f"{t['id']}. [{t.get('category','')}] {t['title']} (исполнитель: {t.get('assignee','')})"
        for t in tasks
    )
    prompt = f"""У нас есть список рабочих задач команды по запуску онлайн-курса.
Сгруппируй их в 5-8 стратегических целей.

Задачи:
{tasks_text}

Верни ТОЛЬКО валидный JSON (без markdown):
{{
  "goals": [
    {{
      "title": "Название цели (глагол + результат, например 'Собрать поток из 20 учеников')",
      "metric": "Как измерим успех (число/факт), или null если нельзя измерить",
      "deadline": "Дедлайн если очевидный, иначе null",
      "category": "обучение/маркетинг/продажи/контент/операционка/продукт",
      "task_ids": [1, 2, 3]
    }}
  ]
}}

Правила:
- Цель = желаемый результат, не процесс
- Одна задача может быть только в одной цели
- Все task_ids должны быть использованы
- Цели должны быть стратегическими, не операционными мелочами
"""
    r = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": "llama-3.3-70b-versatile",
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 2048, "temperature": 0.2},
        timeout=60,
    )
    raw = r.json()["choices"][0]["message"]["content"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def run():
    tasks = get_tasks()
    print(f"Tasks loaded: {len(tasks)}")

    print("Grouping with Groq...")
    result = group_with_groq(tasks)

    task_map = {t["id"]: t for t in tasks}

    for g in result["goals"]:
        print(f"\nGoal: {g['title']}")
        goal_id = create_goal({
            "title": g["title"],
            "metric": g.get("metric"),
            "deadline": g.get("deadline"),
            "status": "active",
            "category": g.get("category", "операционка"),
        })
        print(f"  Created goal id={goal_id}")
        for tid in g.get("task_ids", []):
            if tid in task_map:
                link_task_to_goal(tid, goal_id)
                print(f"  Linked task {tid}: {task_map[tid]['title'][:50]}")

    print("\nDone!")


if __name__ == "__main__":
    run()
