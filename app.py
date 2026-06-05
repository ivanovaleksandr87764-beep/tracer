"""Flask web app — task tracker + Telegram webhook bot."""
import os
import re
import json
import httpx
import logging
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify
import db
from analyze_tasks import analyze_new_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TRACKER_URL = os.environ.get("TRACKER_URL", "")

TASK_KEYWORDS = ["нужно","надо","сделать","сделай","подготовить","добавить",
                 "внедрить","прописать","составить","создать","провести",
                 "запустить","собрать","переделать","записать","поставить"]

TASK_PROMPT = """Ты извлекаешь задачи из рабочих переписок.

Сообщение от {author} ({date}):
{text}

Верни ТОЛЬКО валидный JSON массив (без markdown):
[{{"title":"краткое название","assignee":"{author}","deadline":null,"priority":"high/medium/low","status":"todo","category":"обучение/маркетинг/продажи/контент/операционка/продукт","source_date":"{date}","context":"зачем задача"}}]

Если задач нет — верни []"""


# ── Telegram helpers ─────────────────────────────────────────────────────────

def tg_send(chat_id, text, reply_to=None):
    if not BOT_TOKEN:
        return
    payload = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
               json=payload, timeout=10)


def call_groq(prompt: str, max_tokens: int = 1024) -> str:
    r = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": "llama-3.3-70b-versatile",
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens, "temperature": 0.1},
        timeout=30,
    )
    return r.json()["choices"][0]["message"]["content"].strip()


def parse_json_response(raw: str) -> list | dict:
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


SMART_PROMPT = """Ты не секретарь который пишет задачи под диктовку.
Ты стратегический помощник команды онлайн-курса. Думаешь как сильный консультант: «А зачем это вообще? Нет ли способа лучше? Не отвлекает ли это от главной цели?»

Сообщение от {author} ({date}):
{text}

Текущие активные цели команды:
{goals}

Твоя задача:
1. Понять что человек предлагает сделать
2. КРИТИЧЕСКИ оценить: это правда движет к цели или просто суета?
3. Если задача годная — привязать к существующей цели или создать новую (SMART, с метрикой)
4. Если есть способ эффективнее — предложить альтернативу
5. Если задача сомнительная — задать встречный вопрос или флагнуть

Верни ТОЛЬКО валидный JSON (без markdown):
{{
  "new_goals": [
    {{"title": "цель: глагол + измеримый результат", "metric": "число/конкретный факт", "deadline": "дедлайн или null", "category": "обучение/маркетинг/продажи/контент/операционка/продукт"}}
  ],
  "actions": [
    {{
      "title": "конкретное действие",
      "goal_id": 5,
      "new_goal_index": null,
      "assignee": "{author}",
      "deadline": "дедлайн или null",
      "priority": "high/medium/low",
      "category": "обучение/маркетинг/продажи/контент/операционка/продукт"
    }}
  ],
  "thinking": "Что я думаю об этой задаче (2-3 предложения, по делу, без воды). Например: 'Это под цель X. Но эффективнее сделать Y вместо предложенного. Или вообще пропустить, потому что Z'",
  "alternative": "Если есть способ эффективнее — конкретно опиши его. Иначе пустая строка.",
  "doubt": "Если есть сомнение нужна ли вообще задача — напиши почему. Иначе пустая строка."
}}

Правила:
- Не льсти и не соглашайся со всем подряд. Если задача =суета — так и скажи в doubt.
- Альтернатива должна быть КОНКРЕТНОЙ ("вместо обзвонить 50 лидов — сделать 1 пост который их сам соберёт"), не общими словами.
- Если в сообщении уже описана задача которая дублирует существующую — поставь doubt = "это похоже на задачу #N (название)".
- Метрика обязательна для новой цели. Без метрики — это процесс, а не цель.
- Если задач/целей нет — верни пустые массивы с thinking="не вижу конкретного действия"."""


def smart_extract(text, author, msg_date):
    """Извлекает задачи + привязывает к целям + создаёт новые цели если нужно."""
    if not GROQ_API_KEY:
        return None

    goals = db.get_goals()
    goals_text = "\n".join(
        f"  id={g['id']} [{g.get('category','')}]: {g['title']}" + (f" → метрика: {g['metric']}" if g.get('metric') else "")
        for g in goals if g.get("status") == "active"
    ) or "  (целей пока нет)"

    try:
        raw = call_groq(SMART_PROMPT.format(
            author=author, date=msg_date, text=text[:3000], goals=goals_text
        ), max_tokens=2048)
        return parse_json_response(raw)
    except Exception as e:
        logger.warning(f"Smart extract error: {e}")
        return None


def process_message(text, author, msg_date, chat_id, msg_id):
    """Анализирует сообщение, привязывает к целям, сохраняет."""
    if len(text) < 20:
        return
    if not any(kw in text.lower() for kw in TASK_KEYWORDS):
        return

    result = smart_extract(text, author, msg_date)
    if not result:
        return

    actions = result.get("actions", [])
    new_goals = result.get("new_goals", [])
    thinking = result.get("thinking", "")
    alternative = result.get("alternative", "")
    doubt = result.get("doubt", "")

    # Даже если задач нет — если есть сомнение или альтернатива, ответим
    if not actions and not new_goals and not (doubt or alternative):
        return

    # Создаём новые цели
    created_goals = []
    for ng in new_goals:
        g = db.create_goal({
            "title": ng["title"],
            "metric": ng.get("metric") or None,
            "deadline": ng.get("deadline") or None,
            "status": "active",
            "category": ng.get("category", "операционка"),
        })
        created_goals.append(g)

    # Создаём действия
    saved_tasks = []
    for a in actions:
        goal_id = a.get("goal_id")
        ngi = a.get("new_goal_index")
        if ngi is not None and 0 <= ngi < len(created_goals):
            goal_id = created_goals[ngi].get("id")

        task = {
            "title": a["title"],
            "assignee": a.get("assignee", author),
            "deadline": a.get("deadline"),
            "priority": a.get("priority", "medium"),
            "status": "todo",
            "category": a.get("category", "операционка"),
            "source_date": msg_date,
            "context": a.get("context", ""),
            "goal_id": goal_id,
        }
        saved = db.create_task(task)
        saved_tasks.append({**task, "goal_id": goal_id})

    # Формируем красивый ответ
    msg_parts = []

    if created_goals:
        msg_parts.append("🎯 Новые цели:")
        for g in created_goals:
            line = f"• {g['title']}"
            if g.get("metric"):
                line += f"\n  📊 {g['metric']}"
            if g.get("deadline"):
                line += f"\n  ⏰ {g['deadline']}"
            msg_parts.append(line)
        msg_parts.append("")

    if saved_tasks:
        by_goal = {}
        for t in saved_tasks:
            by_goal.setdefault(t.get("goal_id"), []).append(t)
        goals_map = {g["id"]: g for g in db.get_goals()}

        for gid, tasks in by_goal.items():
            goal = goals_map.get(gid)
            if goal:
                msg_parts.append(f"🎯 К цели «{goal['title']}»:")
            else:
                msg_parts.append("📝 Без цели:")
            for t in tasks:
                line = f"  ✅ {t['title']}"
                if t.get("deadline"):
                    line += f" (до {t['deadline']})"
                msg_parts.append(line)
        msg_parts.append("")

    if thinking:
        msg_parts.append(f"🧠 {thinking}")

    if alternative:
        msg_parts.append(f"\n💡 Альтернатива: {alternative}")

    if doubt:
        msg_parts.append(f"\n⚠️ Сомнение: {doubt}")

    msg_parts.append(f"\n🔗 {TRACKER_URL}")

    tg_send(chat_id, "\n".join(msg_parts), reply_to=msg_id)


def send_deadline_reminders(chat_id):
    """Проверяет дедлайны и шлёт напоминание."""
    tasks = db.get_tasks()
    active = [t for t in tasks if t.get("status") != "done"]
    today = date.today()

    MONTH_MAP = {
        "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
        "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
        "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12,
    }

    def parse_dl(s):
        if not s: return None
        s = s.lower()
        if "конца недели" in s: return today + timedelta(days=(6 - today.weekday()))
        if "конца месяца" in s: return today.replace(day=28)
        if "сегодня" in s: return today
        if "завтра" in s: return today + timedelta(days=1)
        if "эта неделя" in s or "эту неделю" in s: return today + timedelta(days=(4-today.weekday()))
        m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", s)
        if m:
            month = MONTH_MAP.get(m.group(2), 0)
            if month:
                try: return date(int(m.group(3)), month, int(m.group(1)))
                except: pass
        return None

    overdue, due_today, due_soon = [], [], []
    for t in active:
        dl = parse_dl(t.get("deadline", ""))
        if not dl: continue
        if dl < today: overdue.append(t)
        elif dl == today: due_today.append(t)
        elif dl <= today + timedelta(days=3): due_soon.append(t)

    parts = []
    if overdue:
        lines = "\n".join(f"🔴 {t['title'][:50]} ({t.get('assignee','')})" for t in overdue)
        parts.append(f"🚨 ПРОСРОЧЕНО:\n{lines}")
    if due_today:
        lines = "\n".join(f"🟡 {t['title'][:50]} ({t.get('assignee','')})" for t in due_today)
        parts.append(f"⏰ Дедлайн СЕГОДНЯ:\n{lines}")
    if due_soon:
        lines = "\n".join(f"🟠 {t['title'][:50]} ({t.get('assignee','')})" for t in due_soon)
        parts.append(f"📅 Скоро (3 дня):\n{lines}")

    if parts:
        tg_send(chat_id, "\n\n".join(parts) + f"\n\n{TRACKER_URL}")
    else:
        tg_send(chat_id, f"✅ Просроченных задач нет!\n\n{TRACKER_URL}")


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.route("/webhook/<path:token>", methods=["POST"])
def webhook(token):
    data = request.json
    if not data:
        return "ok"

    msg = data.get("message") or data.get("channel_post")
    if not msg or not msg.get("text"):
        return "ok"

    text = msg["text"]
    chat_id = msg["chat"]["id"]
    msg_id = msg["message_id"]
    user = msg.get("from", {})
    author = user.get("first_name", "") + (" " + user.get("last_name", "")).strip()
    msg_date = datetime.now().strftime("%d %B %Y")

    # Команды
    if text.startswith("/start") or text.startswith("/tasks"):
        tg_send(chat_id, f"📋 Трекер задач: {TRACKER_URL}")
        return "ok"

    if text.startswith("/remind"):
        send_deadline_reminders(chat_id)
        return "ok"

    if text.startswith("/add "):
        title = text[5:].strip()
        if title:
            db.create_task({"title": title, "assignee": author,
                            "deadline": None, "priority": "medium",
                            "status": "todo", "category": "операционка",
                            "source_date": msg_date, "context": "Добавлено через бота"})
            tg_send(chat_id, f"✅ Задача добавлена: {title}", reply_to=msg_id)
        return "ok"

    # Анализ обычного сообщения
    process_message(text, author, msg_date, chat_id, msg_id)
    return "ok"


@app.route("/api/setup-webhook", methods=["POST"])
def setup_webhook():
    """Регистрирует webhook в Telegram — вызвать один раз после деплоя."""
    if not BOT_TOKEN or not TRACKER_URL:
        return jsonify({"error": "BOT_TOKEN or TRACKER_URL not set"}), 400
    webhook_url = f"{TRACKER_URL}/webhook/{BOT_TOKEN}"
    r = httpx.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
        json={"url": webhook_url, "allowed_updates": ["message", "channel_post"]},
        timeout=10,
    )
    return jsonify(r.json())


# ── Web API ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/tasks")
def get_tasks():
    filters = {}
    for key in ("status", "assignee", "priority", "category"):
        val = request.args.get(key)
        if val:
            filters[key] = val
    tasks = db.get_tasks(filters)
    search = request.args.get("search", "").lower()
    if search:
        tasks = [t for t in tasks if search in t.get("title", "").lower()
                 or search in t.get("context", "").lower()]
    return jsonify(tasks)


@app.route("/api/tasks/<int:task_id>", methods=["PATCH"])
def update_task(task_id):
    result = db.update_task(task_id, request.json)
    return jsonify(result) if result else (jsonify({"error": "not found"}), 404)


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    db.delete_task(task_id)
    return jsonify({"ok": True})


@app.route("/api/tasks", methods=["POST"])
def create_task():
    data = request.json
    task = {
        "title": data.get("title", ""),
        "assignee": data.get("assignee", ""),
        "deadline": data.get("deadline"),
        "priority": data.get("priority", "medium"),
        "status": data.get("status", "todo"),
        "category": data.get("category", "операционка"),
        "source_date": datetime.now().strftime("%d %B %Y"),
        "context": data.get("context", "Добавлено вручную"),
    }
    return jsonify(db.create_task(task))


@app.route("/api/analyze", methods=["POST"])
def analyze_message():
    data = request.json
    message = {"id": "manual", "date": datetime.now().strftime("%d %B %Y"),
                "time": datetime.now().strftime("%H:%M"),
                "author": data.get("author", ""), "text": data.get("text", "")}
    new_tasks = analyze_new_message(message, save_fn=db.create_task)
    return jsonify(new_tasks)


@app.route("/api/assignees")
def get_assignees():
    tasks = db.get_tasks()
    return jsonify(sorted({t.get("assignee", "") for t in tasks if t.get("assignee")}))


@app.route("/api/stats")
def get_stats():
    return jsonify(db.get_stats())


# ── Goals API ─────────────────────────────────────────────────────────────────

@app.route("/api/goals")
def get_goals():
    return jsonify(db.get_goals_with_tasks())


@app.route("/api/goals", methods=["POST"])
def create_goal():
    data = request.json
    goal = {
        "title": data.get("title", ""),
        "metric": data.get("metric") or None,
        "deadline": data.get("deadline") or None,
        "status": "active",
        "category": data.get("category", "операционка"),
    }
    return jsonify(db.create_goal(goal))


@app.route("/api/goals/<int:goal_id>", methods=["PATCH"])
def update_goal(goal_id):
    result = db.update_goal(goal_id, request.json)
    return jsonify(result) if result else (jsonify({"error": "not found"}), 404)


@app.route("/api/goals/<int:goal_id>", methods=["DELETE"])
def delete_goal(goal_id):
    db.delete_goal(goal_id)
    return jsonify({"ok": True})


@app.route("/api/goal-stats")
def goal_stats():
    return jsonify(db.get_goal_stats())


@app.route("/api/migrate", methods=["POST"])
def migrate():
    db.migrate_from_json()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
