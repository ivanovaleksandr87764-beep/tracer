"""Analyze chat messages and extract tasks via Groq (free) or keyword fallback."""
import json
import os
import re
import httpx
from pathlib import Path

BACKLOG_FILE = Path(__file__).parent / "backlog.json"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

TASK_KEYWORDS = ["нужно","надо","сделать","сделай","подготовить","добавить",
                 "внедрить","прописать","составить","создать","провести",
                 "запустить","собрать","переделать","записать","поставить"]

PROMPT = """Ты извлекаешь задачи из рабочих переписок команды.

Сообщения:
{text}

Верни ТОЛЬКО валидный JSON массив (без markdown):
[{{
  "title": "краткое конкретное название",
  "assignee": "имя если указан, иначе ''",
  "deadline": "дедлайн если упомянут, иначе null",
  "priority": "high/medium/low",
  "status": "todo/in_progress/done",
  "category": "обучение/маркетинг/продажи/контент/операционка/продукт",
  "source_date": "дата сообщения",
  "context": "зачем задача (1 предложение)"
}}]

Правила:
- Только конкретные действия, не обсуждения
- priority=high если есть дедлайн или слова срочно/важно
- status=done если в тексте написано что уже сделано
- Если задач нет — верни []"""


def extract_tasks_from_messages(messages: list[dict]) -> list[dict]:
    text = "\n\n".join(
        f"[{m.get('date','')} | {m.get('author','')}]\n{m['text']}"
        for m in messages if m.get("text")
    )

    if not any(kw in text.lower() for kw in TASK_KEYWORDS):
        return []

    if GROQ_API_KEY:
        try:
            r = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": "llama-3.3-70b-versatile",
                      "messages": [{"role": "user", "content": PROMPT.format(text=text[:4000])}],
                      "max_tokens": 2048, "temperature": 0.1},
                timeout=30,
            )
            raw = r.json()["choices"][0]["message"]["content"].strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            result = json.loads(raw)
            return result if isinstance(result, list) else []
        except Exception as e:
            print(f"Groq error: {e}")

    return []


def load_backlog() -> list[dict]:
    if BACKLOG_FILE.exists():
        with open(BACKLOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_backlog(tasks: list[dict]) -> None:
    with open(BACKLOG_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def assign_ids(tasks: list[dict], start_id: int) -> list[dict]:
    for i, t in enumerate(tasks):
        t["id"] = start_id + i
    return tasks


def analyze_new_message(message: dict, save_fn=None) -> list[dict]:
    new_tasks = extract_tasks_from_messages([message])
    if save_fn:
        for task in new_tasks:
            save_fn(task)
    else:
        backlog = load_backlog()
        next_id = max((t.get("id", 0) for t in backlog), default=0) + 1
        new_tasks = assign_ids(new_tasks, next_id)
        backlog.extend(new_tasks)
        save_backlog(backlog)
    return new_tasks
