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
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TRACKER_URL = os.environ.get("TRACKER_URL", "http://localhost:5000")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")  # бесплатно: console.groq.com

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


TASK_PROMPT = """Ты — ассистент который извлекает задачи из рабочих переписок.

Сообщение от {author} ({date}):
{text}

Извлеки конкретные задачи. Верни ТОЛЬКО валидный JSON массив (без markdown, без пояснений):
[{{"title": "краткое название", "assignee": "имя или ''", "deadline": "дедлайн или null", "priority": "high/medium/low", "status": "todo", "category": "обучение/маркетинг/продажи/контент/операционка/продукт", "source_date": "{date}", "context": "зачем задача"}}]

Если задач нет — верни []"""


async def _call_groq(prompt: str) -> str:
    """Вызов Groq API (бесплатный Llama 3.3)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1024,
                "temperature": 0.1,
            },
        )
        return r.json()["choices"][0]["message"]["content"]


async def _call_anthropic(prompt: str) -> str:
    """Вызов Claude API."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _parse_ai_response(raw: str) -> list[dict]:
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    tasks = json.loads(raw)
    return tasks if isinstance(tasks, list) else []


async def extract_tasks_ai(text: str, author: str, msg_date: str) -> list[dict]:
    """Извлечение задач через AI (Groq → Claude → keyword fallback)."""
    prompt = TASK_PROMPT.format(author=author, date=msg_date, text=text)

    # 1. Groq (бесплатно)
    if GROQ_API_KEY:
        try:
            raw = await _call_groq(prompt)
            return _parse_ai_response(raw)
        except Exception as e:
            logger.warning(f"Groq error: {e}")

    # 2. Anthropic Claude (если есть ключ)
    if ANTHROPIC_API_KEY:
        try:
            raw = await _call_anthropic(prompt)
            return _parse_ai_response(raw)
        except Exception as e:
            logger.warning(f"Claude error: {e}")

    # 3. Keyword fallback (без API)
    return extract_tasks_local(text, author, msg_date)


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

    tasks = await extract_tasks_ai(text, author, date)

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
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Открыть трекер", web_app=WebAppInfo(url=TRACKER_URL))
    ]])
    await update.message.reply_text(
        "👋 Привет! Слежу за чатом и автоматически записываю задачи в трекер.\n\n"
        "Просто пишите как обычно — если увижу задачу, сразу добавлю.\n\n"
        "Команды:\n"
        "/tasks — открыть трекер\n"
        "/add Текст — добавить задачу вручную\n"
        "/remind — проверить дедлайны",
        reply_markup=keyboard,
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Открыть трекер", web_app=WebAppInfo(url=TRACKER_URL))
    ]])
    await update.message.reply_text("Открывай 👇", reply_markup=keyboard)


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


CHAT_ID = os.environ.get("REMINDER_CHAT_ID", "")  # ID чата куда слать напоминания

# Дедлайн-парсеры — из текста в date
DEADLINE_PARSE_RULES = [
    # "до конца недели" → ближайшее воскресенье
    (r"до\s+конца\s+недели", lambda: (date.today() + timedelta(days=(6 - date.today().weekday())))),
    # "до конца месяца"
    (r"до\s+конца\s+месяца", lambda: date.today().replace(day=28)),
    # "сегодня"
    (r"сегодня", lambda: date.today()),
    # "завтра"
    (r"завтра", lambda: date.today() + timedelta(days=1)),
    # "эта неделя"
    (r"эта\s+недел", lambda: date.today() + timedelta(days=(4 - date.today().weekday()))),
    # "следующая неделя"
    (r"следующая\s+недел", lambda: date.today() + timedelta(days=(11 - date.today().weekday()))),
    # "середина следующей недели"
    (r"середина\s+следующей", lambda: date.today() + timedelta(days=(9 - date.today().weekday()))),
    # "пятница"
    (r"пятниц", lambda: date.today() + timedelta(days=(4 - date.today().weekday()) % 7)),
    # "2 June 2026" или "2 июня 2026"
    (r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December|января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})",
     lambda m=None: _parse_full_date(m)),
]

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _parse_full_date(m) -> date | None:
    if not m:
        return None
    try:
        day = int(m.group(1))
        month = MONTH_MAP.get(m.group(2).lower(), 0)
        year = int(m.group(3))
        return date(year, month, day)
    except Exception:
        return None


def parse_deadline(deadline_str: str) -> date | None:
    """Парсит строку дедлайна в объект date."""
    if not deadline_str:
        return None
    for pattern, resolver in DEADLINE_PARSE_RULES:
        m = re.search(pattern, deadline_str, re.IGNORECASE)
        if m:
            try:
                if pattern.startswith(r"(\d{1,2})"):
                    result = _parse_full_date(m)
                else:
                    result = resolver()
                return result
            except Exception:
                continue
    return None


async def get_all_tasks() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{TRACKER_URL}/api/tasks")
            return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def format_reminder(tasks: list[dict], label: str) -> str:
    if not tasks:
        return ""
    lines = [f"⏰ *{label}*"]
    for t in tasks:
        assignee = f" (@{t['assignee']})" if t.get("assignee") else ""
        status_icon = {"todo": "🔴", "in_progress": "🟡", "done": "✅"}.get(t.get("status"), "⚪")
        lines.append(f"{status_icon} {t['title'][:70]}{assignee}")
    return "\n".join(lines)


async def send_deadline_reminders(context) -> None:
    """Проверяет дедлайны и шлёт напоминания в чат."""
    chat_id = CHAT_ID
    if not chat_id:
        logger.warning("REMINDER_CHAT_ID не задан — напоминания не отправляются")
        return

    tasks = await get_all_tasks()
    active = [t for t in tasks if t.get("status") != "done"]

    today = date.today()
    tomorrow = today + timedelta(days=1)

    overdue, due_today, due_tomorrow, due_week = [], [], [], []

    for t in active:
        dl = parse_deadline(t.get("deadline", ""))
        if not dl:
            continue
        if dl < today:
            overdue.append(t)
        elif dl == today:
            due_today.append(t)
        elif dl == tomorrow:
            due_tomorrow.append(t)
        elif today < dl <= today + timedelta(days=7):
            due_week.append(t)

    parts = []
    if overdue:
        parts.append(format_reminder(overdue, "🚨 ПРОСРОЧЕНО"))
    if due_today:
        parts.append(format_reminder(due_today, "Дедлайн СЕГОДНЯ"))
    if due_tomorrow:
        parts.append(format_reminder(due_tomorrow, "Дедлайн ЗАВТРА"))
    if due_week:
        parts.append(format_reminder(due_week, "Дедлайн на этой неделе"))

    if not parts:
        return

    message = "\n\n".join(parts) + f"\n\n🔗 {TRACKER_URL}"
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown",
        )
        logger.info(f"Sent deadline reminder: {len(overdue)} overdue, {len(due_today)} today, {len(due_tomorrow)} tomorrow")
    except Exception as e:
        logger.error(f"Failed to send reminder: {e}")


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительно проверить дедлайны прямо сейчас."""
    tasks = await get_all_tasks()
    active = [t for t in tasks if t.get("status") != "done"]

    today = date.today()
    tomorrow = today + timedelta(days=1)

    overdue, due_today, due_tomorrow, due_week = [], [], [], []
    no_deadline = []

    for t in active:
        dl = parse_deadline(t.get("deadline", ""))
        if not dl:
            no_deadline.append(t)
            continue
        if dl < today:
            overdue.append(t)
        elif dl == today:
            due_today.append(t)
        elif dl == tomorrow:
            due_tomorrow.append(t)
        elif today < dl <= today + timedelta(days=7):
            due_week.append(t)

    parts = []
    if overdue:
        parts.append(format_reminder(overdue, "🚨 ПРОСРОЧЕНО"))
    if due_today:
        parts.append(format_reminder(due_today, "Дедлайн СЕГОДНЯ"))
    if due_tomorrow:
        parts.append(format_reminder(due_tomorrow, "Дедлайн ЗАВТРА"))
    if due_week:
        parts.append(format_reminder(due_week, "Дедлайн на этой неделе"))
    if not parts:
        parts.append("✅ Просроченных и горящих задач нет!")

    parts.append(f"\n📊 Без дедлайна: {len(no_deadline)} задач")
    await update.message.reply_text("\n\n".join(parts) + f"\n\n🔗 {TRACKER_URL}", parse_mode="Markdown")


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
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Напоминания каждый день в 09:00
    job_queue = app.job_queue
    job_queue.run_daily(
        send_deadline_reminders,
        time=datetime.strptime("09:00", "%H:%M").time(),
        name="daily_reminder",
    )
    # Доп. проверка в 18:00 — только просроченные и сегодняшние
    job_queue.run_daily(
        send_deadline_reminders,
        time=datetime.strptime("18:00", "%H:%M").time(),
        name="evening_reminder",
    )

    # Ставим кнопку меню (📋) рядом с полем ввода — открывает мини-апп
    async def post_init(app):
        if TRACKER_URL and TRACKER_URL.startswith("https://"):
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="📋 Трекер", web_app=WebAppInfo(url=TRACKER_URL))
            )

    app.post_init = post_init

    print(f"🤖 Бот запущен. Трекер: {TRACKER_URL}")
    print(f"⏰ Напоминания: 09:00 и 18:00 → chat_id={CHAT_ID or 'не задан'}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
