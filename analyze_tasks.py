"""Analyze chat messages and extract tasks using Claude API."""
import json
import os
import re
from pathlib import Path
import anthropic

BACKLOG_FILE = Path(__file__).parent / "backlog.json"
MESSAGES_FILE = Path(__file__).parent / "messages.json"

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """Ты аналитик рабочего чата команды запуска онлайн-курса.
Твоя задача — извлекать КОНКРЕТНЫЕ задачи из переписки, особенно связанные с продуктом, обучением, контентом и улучшениями курса.

Формат ответа — ТОЛЬКО валидный JSON массив задач (без markdown, без ```json), каждая задача:
{
  "title": "краткое чёткое название задачи",
  "assignee": "имя исполнителя если указан, иначе ''",
  "deadline": "дедлайн если упомянут (дата или 'до конца недели' и т.д.), иначе null",
  "priority": "high/medium/low",
  "status": "todo/in_progress/done",
  "category": "обучение/маркетинг/продажи/операционка/контент/продукт",
  "source_date": "дата сообщения",
  "context": "1-2 предложения зачем эта задача"
}

Правила:
- Ищи задачи типа: "нужно сделать X", "сделай X", "добавить X в курс", "внедрить X", "записать урок", "подготовить модуль"
- Особый фокус: задачи по улучшению курса/обучения — новые модули, уроки, материалы, методологии
- Ставь статус done если в том же сообщении написано что уже сделано
- Ставь priority=high если есть дедлайн или слова "срочно/важно/критично"
- Не извлекай: обсуждения без конкретного действия, вопросы без ответа-задачи, просто идеи без назначения
- Если задач нет — верни []
"""


def extract_tasks_from_messages(messages: list[dict]) -> list[dict]:
    """Send batch of messages to Claude and get extracted tasks."""
    text = "\n\n".join(
        f"[{m['date']} {m['time'][:5] if m['time'] else ''} | {m['author']}]\n{m['text']}"
        for m in messages
        if m.get("text")
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Извлеки задачи из этих сообщений:\n\n{text}"}],
    )

    raw = response.content[0].text.strip()
    # strip markdown code fences if present
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        tasks = json.loads(raw)
        return tasks if isinstance(tasks, list) else []
    except json.JSONDecodeError:
        print(f"Failed to parse JSON: {raw[:200]}")
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


def analyze_full_chat(batch_size: int = 30) -> list[dict]:
    """Analyze all messages from chat export in batches."""
    with open(MESSAGES_FILE, encoding="utf-8") as f:
        messages = json.load(f)

    print(f"Total messages to analyze: {len(messages)}")
    all_tasks = []

    for i in range(0, len(messages), batch_size):
        batch = messages[i: i + batch_size]
        print(f"Processing batch {i // batch_size + 1}/{(len(messages) + batch_size - 1) // batch_size}...")
        tasks = extract_tasks_from_messages(batch)
        print(f"  Found {len(tasks)} tasks")
        all_tasks.extend(tasks)

    all_tasks = assign_ids(all_tasks, 1)
    save_backlog(all_tasks)
    print(f"\nTotal tasks extracted: {len(all_tasks)}")
    print(f"Saved to {BACKLOG_FILE}")
    return all_tasks


def analyze_new_message(message: dict, save_fn=None) -> list[dict]:
    """Analyze a single new message and add tasks to backlog.
    save_fn — функция сохранения (db.create_task или save_backlog).
    """
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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--full":
        analyze_full_chat()
    else:
        print("Usage: python analyze_tasks.py --full")
        print("This will analyze all chat messages and extract tasks.")
