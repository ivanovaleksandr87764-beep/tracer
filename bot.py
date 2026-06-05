"""
Telegram bot — real-time task extractor.
Мониторит сообщения в чате, находит задачи и добавляет в трекер.

Запуск: python bot.py
"""
import os
import re
import json
import logging
import httpx
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TRACKER_URL = os.environ.get("TRACKER_URL", "http://localhost:5000")  # URL трекера (Railway или localhost)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Ключевые слова для обнаружения задач
TASK_PATTERNS = [
    r"нужно\s+(.+)",
    r"надо\s+(.+)",
    r"сделать\s+(.+)",
    r"сделай\s+(.+)",
    r"подготовить\s+(.+)",
    r"подготовь\s+(.+)",
    r"записать\s+(.+)",
    r"запиши\s+(.+)",
    r"добавить\s+(.+)",
    r"добавь\s+(.+)",
    r"внедрить\s+(.+)",
    r"внедри\s+(.+)",
    r"прописать\s+(.+)",
    r"пропиши\s+(.+)",
    r"составить\s+(.+)",
    r"составь\s+(.+)",
    r"создать\s+(.+)",
    r"создай\s+(.+)",
    r"провести\s+(.+)",
    r"проведи\s+(.+)",
    r"запустить\s+(.+)",
    r"запусти\s+(.+)",
    r"собрать\s+(.+)",
    r"собери\s+(.+)",
    r"переделать\s+(.+)",
    r"переделай\s+(.+)",
    r"починить\s+(.+)",
    r"почини\s+(.+)",
]

DEADLINE_PATTERNS = [
    (r"до\s+(конца\s+недели|конца\s+месяца)", lambda m: m.group(1)),
    (r"до\s+(\d{1,2}[./]\d{1,2})", lambda m: m.group(1)),
    (r"(сегодня|завтра|послезавтра)", lambda m: m.group(1)),
    (r"в\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)", lambda m: m.group(1)),
    (r"на\s+(этой|следующей)\s+неделе", lambda m: m.group(1) + " неделе"),
]

PRIORITY_HIGH = ["срочно", "важно", "критично", "горит", "asap", "асап", "сегодня", "прямо сейчас"]
PRIORITY_LOW = ["когда-нибудь", "потом", "в будущем", "при случае", "не срочно"]

CATEGORY_KEYWORDS = {
    "обучение": ["урок", "модуль", "курс", "ученик", "обучение", "созвон", "зум", "zoom", "программа", "материал", "контент курс"],
    "маркетинг": ["реклама", "трафик", "воронка", "подписчик", "канал", "закреп", "бот", "кастдев", "анализ конкурент"],
    "продажи": ["лид", "продажа", "продать", "закрыть", "дожать", "созвон с клиент", "заявка"],
    "контент": ["пост", "пишем", "написать", "контент", "рилс", "ютуб", "видео", "тредс", "контент-план"],
    "операционка": ["расписание", "таблица", "реестр", "чат", "куратор", "роли", "система"],
    "продукт": ["продукт", "программа", "структура", "методолог", "артефакт"],
}


def detect_priority(text: str) -> str:
    text_lower = text.lower()
    if any(w in text_lower for w in PRIORITY_HIGH):
        return "high"
    if any(w in text_lower for w in PRIORITY_LOW):
        return "low"
    return "medium"


def detect_deadline(text: str) -> str | None:
    for pattern, extractor in DEADLINE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return extractor(m)
    return None


def detect_category(text: str) -> str:
    text_lower = text.lower()
    scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "операционка"


def extract_tasks_local(text: str, author: str, date: str) -> list[dict]:
    """Keyword-based task extraction (без API)."""
    tasks = []
    lines = text.split("\n")

    for line in lines:
        line = line.strip()
        if len(line) < 10:
            continue

        for pattern in TASK_PATTERNS:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                title = line[:120]
                tasks.append({
                    "title": title,
                    "assignee": author,
                    "deadline": detect_deadline(text),
                    "priority": detect_priority(text),
                    "status": "todo",
                    "category": detect_category(text),
                    "source_date": date,
                    "context": f"Из сообщения: {text[:200]}",
                })
                break  # одна задача на строку

    return tasks


async def extract_tasks_claude(text: str, author: str, date: str) -> list[dict]:
    """Task extraction via Claude API."""
    if not ANTHROPIC_API_KEY:
        return extract_tasks_local(text, author, date)

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Сообщение от {author} ({date}):
{text}

Извлеки задачи. Верни ТОЛЬКО JSON массив (без markdown):
[{{"title": "...", "assignee": "{author}", "deadline": null, "priority": "high/medium/low", "status": "todo", "category": "обучение/маркетинг/продажи/контент/операционка/продукт", "source_date": "{date}", "context": "..."}}]
Если задач нет — верни []"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        tasks = json.loads(raw)
        return tasks if isinstance(tasks, list) else []
    except Exception as e:
        logger.warning(f"Claude API error: {e}, falling back to local")
        return extract_tasks_local(text, author, date)


async def add_task_to_tracker(task: dict) -> dict | None:
    """POST task to the tracker API."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{TRACKER_URL}/api/tasks", json=task)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.error(f"Failed to add task to tracker: {e}")
    return None


# ─── Bot handlers ───────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text
    author = ""
    if msg.from_user:
        author = msg.from_user.full_name or msg.from_user.username or ""
    date = msg.date.strftime("%d %B %Y") if msg.date else datetime.now().strftime("%d %B %Y")

    # Быстрая проверка — есть ли вообще признаки задачи
    task_keywords = ["нужно", "надо", "сделать", "сделай", "подготовить", "добавить",
                     "внедрить", "прописать", "составить", "создать", "провести",
                     "запустить", "собрать", "переделать", "записать"]
    if not any(kw in text.lower() for kw in task_keywords):
        return

    if len(text) < 20:
        return

    logger.info(f"Potential task message from {author}: {text[:80]}")

    tasks = await extract_tasks_claude(text, author, date)

    if not tasks:
        return

    added = []
    for task in tasks:
        result = await add_task_to_tracker(task)
        if result:
            added.append(result)

    if added and update.effective_chat:
        titles = "\n".join(f"• {t['title'][:60]}" for t in added)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📋 Добавил {len(added)} задач{'у' if len(added)==1 else 'и'} в трекер:\n{titles}",
            reply_to_message_id=msg.message_id,
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я слежу за сообщениями в чате и автоматически добавляю задачи в трекер.\n\n"
        f"🔗 Трекер: {TRACKER_URL}\n\n"
        "Просто пишите в чат как обычно — если увижу задачу, сразу запишу."
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать ссылку на трекер."""
    await update.message.reply_text(f"📋 Трекер задач: {TRACKER_URL}")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вручную добавить задачу: /add Название задачи"""
    if not context.args:
        await update.message.reply_text("Использование: /add Название задачи")
        return

    title = " ".join(context.args)
    author = update.message.from_user.full_name if update.message.from_user else ""
    task = {
        "title": title,
        "assignee": author,
        "deadline": None,
        "priority": "medium",
        "status": "todo",
        "category": detect_category(title),
        "source_date": datetime.now().strftime("%d %B %Y"),
        "context": "Добавлено вручную через бота",
    }
    result = await add_task_to_tracker(task)
    if result:
        await update.message.reply_text(f"✅ Задача добавлена: {title}")
    else:
        await update.message.reply_text("❌ Не удалось добавить задачу — трекер недоступен")


def main():
    token = BOT_TOKEN
    if not token:
        print("ERROR: Задай TELEGRAM_BOT_TOKEN")
        print("  $env:TELEGRAM_BOT_TOKEN = '1234567890:ABC...'")
        return

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"🤖 Бот запущен. Трекер: {TRACKER_URL}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
