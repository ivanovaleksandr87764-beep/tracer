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

TASK_KEYWORDS = ["нужно","надо","сделать","сделай","сделал","сделала","сделано","сделана",
                 "подготовить","подготовил","готово","готова","закрыл","закрыла","закрыто",
                 "добавить","добавил","внедрить","внедрил","прописать","прописал",
                 "составить","составил","создать","создал","провести","провёл","провел",
                 "запустить","запустил","собрать","собрал","переделать","записать","записал",
                 "поставить","поставил","взял","взяла","начал","начала","приступаю","закончил","выполнил"]

TASK_PROMPT = """Ты извлекаешь задачи из рабочих переписок.

Сообщение от {author} ({date}):
{text}

Верни ТОЛЬКО валидный JSON массив (без markdown):
[{{"title":"краткое название","assignee":"{author}","deadline":null,"priority":"high/medium/low","status":"todo","category":"обучение/маркетинг/продажи/контент/операционка/продукт","source_date":"{date}","context":"зачем задача"}}]

Если задач нет — верни []"""


# ── Telegram helpers ─────────────────────────────────────────────────────────

def tg_send(chat_id, text, reply_to=None, markdown=True):
    if not BOT_TOKEN:
        return
    payload = {"chat_id": chat_id, "text": text}
    if markdown:
        payload["parse_mode"] = "Markdown"
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        r = httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                       json=payload, timeout=10)
        # Если markdown сломал — пробуем без него
        if markdown and r.status_code != 200:
            payload.pop("parse_mode", None)
            httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                       json=payload, timeout=10)
    except Exception as e:
        logger.warning(f"tg_send error: {e}")


AI_MODEL = os.environ.get("AI_MODEL", "openai/gpt-oss-120b")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3-turbo")


def transcribe_voice(file_id: str) -> str:
    """Скачивает голосовое и транскрибирует через Groq Whisper."""
    try:
        # 1. Получаем путь к файлу
        r = httpx.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                      params={"file_id": file_id}, timeout=10)
        file_path = r.json()["result"]["file_path"]

        # 2. Скачиваем
        audio = httpx.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
            timeout=30,
        ).content

        # 3. Распознаём через Groq Whisper (бесплатно)
        r = httpx.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("audio.ogg", audio, "audio/ogg")},
            data={"model": WHISPER_MODEL, "language": "ru"},
            timeout=60,
        )
        return r.json().get("text", "")
    except Exception as e:
        logger.warning(f"Voice transcribe error: {e}")
        return ""


def call_groq(prompt: str, max_tokens: int = 1024, temperature: float = 0.1) -> str:
    r = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": AI_MODEL,
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens, "temperature": temperature},
        timeout=60,
    )
    content = r.json()["choices"][0]["message"]["content"].strip()
    # DeepSeek R1 модели возвращают <think>...</think> блок — убираем его
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
    return content.strip()


def parse_json_response(raw: str) -> list | dict:
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


SMART_PROMPT = """Ты не секретарь. Ты стратегический партнёр команды онлайн-курса.
Думаешь как сильный консультант: задаёшь неудобные вопросы, ищешь более эффективные пути, не боишься сказать "это суета".

Сообщение от {author} ({date}):
{text}

Текущие активные цели команды:
{goals}

Существующие задачи (для апдейта статусов):
{tasks}

ТВОЙ ПРОЦЕСС ДУМАНИЯ (важен порядок):
1. ДОСТАТОЧНО ДАННЫХ? Если в сообщении нет ключевых деталей (что именно / для какой аудитории / срок / зачем) — НЕ ДОДУМЫВАЙ. Задай встречные вопросы в thinking.
2. РЕЗУЛЬТАТ. Какой реальный результат человек хочет? (не задачу — а конечный результат)
3. ЦЕЛЬ. К какой стратегической цели это движет?
4. ПУТЬ. Тот ли путь выбран? Что если сделать наоборот / меньше / по-другому?
5. ЦЕНА И РИСКИ. Сколько съест времени? Что упустим если возьмём?
6. ИТОГ. Брать / переделать / выкинуть / уточнить.

В thinking — связный ход мысли, как живой консультант. 3-5 предложений. Без буллетов. БЕЗ ВЫДУМЫВАНИЯ деталей которых не было в сообщении.

КРИТИЧНО про додумывание:
- Если человек написал "сделай контент-план" — НЕ ПИШИ "на месяц". Спроси: "На какой период? Для какого канала?"
- Если "обзвонить лидов" — не пиши конкретные цифры если их не было
- Лучше задать вопрос чем придумать контекст

Верни ТОЛЬКО валидный JSON (без markdown):
{{
  "updates": [
    {{"task_id": 35, "new_status": "done", "comment": "пользователь сказал что выполнил"}}
  ],
  "new_goals": [
    {{"title": "цель: глагол + измеримый результат", "metric": "число/конкретный факт", "deadline": "дедлайн или null", "category": "обучение/маркетинг/продажи/контент/операционка/продукт"}}
  ],
  "actions": [
    {{
      "title": "конкретное действие",
      "goal_id": 5,
      "goal_title": "Название цели (для отображения)",
      "new_goal_index": null,
      "assignee": "{author}",
      "deadline": "дедлайн или null",
      "priority": "high/medium/low",
      "category": "обучение/маркетинг/продажи/контент/операционка/продукт",
      "context": "ЗАЧЕМ это действие — одно короткое предложение которое связывает действие с метрикой цели"
    }}
  ],
  "thinking": "Ход мысли 3-5 предложений. Если данных МАЛО — задай 2-3 уточняющих вопроса вместо размышлений. Если данных ДОСТАТОЧНО — рассуждай по существу. БЕЗ выдумывания цифр/сроков/деталей которых не было.",
  "questions": ["Уточняющий вопрос 1", "Вопрос 2"],
  "alternative": "Если видишь более эффективный путь И есть достаточно данных — конкретно опиши. Иначе пустая строка.",
  "doubt": "Если задача дублирует существующую или вообще не нужна — напиши почему, название цели в кавычках. Иначе пустая строка."
}}

ОБНОВЛЕНИЯ СТАТУСОВ (updates) — самое важное:
- Если автор пишет "таблица сделана" / "готово" / "закрыл" / "сделал" — найди соответствующую задачу в списке и поставь new_status="done"
- Если пишет "взял в работу" / "начал делать" / "приступаю" / "сейчас занимаюсь" — поставь new_status="in_progress"
- Если "вернул в работу" / "не получилось" — new_status="todo"
- Используй точные task_id из списка задач выше
- Если непонятно про какую именно задачу — задай уточняющий вопрос в questions, updates оставь пустым
- НЕ создавай новую задачу если речь об обновлении существующей

ПРАВИЛА:
- НИКОГДА не пиши "id5", "цель #3" — только НАЗВАНИЕ цели в кавычках
- Не льсти. Не соглашайся со всем. Если суета — так и скажи в doubt
- Альтернатива должна быть КОНКРЕТНОЙ ("сделай X вместо Y потому что Z"), не общими фразами
- Метрика для новой цели обязательна — без метрики это процесс
- Думай как Шамиль или Тимур: что реально двигает запуск, а что съест ресурсы зря"""


def smart_extract(text, author, msg_date):
    """Извлекает задачи + привязывает к целям + апдейтит статусы."""
    if not GROQ_API_KEY:
        return None

    goals = db.get_goals()
    goals_text = "\n".join(
        f"  id={g['id']} [{g.get('category','')}]: {g['title']}" + (f" → метрика: {g['metric']}" if g.get('metric') else "")
        for g in goals if g.get("status") == "active"
    ) or "  (целей пока нет)"

    # Передаём активные задачи чтобы AI мог апдейтить их статусы
    all_tasks = db.get_tasks()
    active_tasks = [t for t in all_tasks if t.get("status") != "done"]
    # Берём задачи назначенные на автора + последние общие, чтобы влезть в контекст
    author_lower = author.lower().split()[0] if author else ""
    mine = [t for t in active_tasks if author_lower and t.get("assignee","").lower().find(author_lower) >= 0]
    others = [t for t in active_tasks if t not in mine][:15]
    relevant = mine + others
    tasks_text = "\n".join(
        f"  id={t['id']} [{t.get('status','todo')}]: {t['title']} (исполнитель: {t.get('assignee','')})"
        for t in relevant
    ) or "  (активных задач нет)"

    try:
        raw = call_groq(SMART_PROMPT.format(
            author=author, date=msg_date, text=text[:6000],
            goals=goals_text, tasks=tasks_text,
        ), max_tokens=3000, temperature=0.4)
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
    updates = result.get("updates", []) or []
    thinking = result.get("thinking", "")
    alternative = result.get("alternative", "")
    doubt = result.get("doubt", "")
    questions = result.get("questions", []) or []

    # Применяем обновления статусов
    updated_tasks = []
    for u in updates:
        tid = u.get("task_id")
        status = u.get("new_status")
        if tid and status in ("todo", "in_progress", "done"):
            updated = db.update_task(tid, {"status": status})
            if updated:
                updated_tasks.append({**updated, "_new_status": status, "_comment": u.get("comment","")})

    # Если только обновления статусов — этого достаточно для ответа
    if not actions and not new_goals and not updated_tasks and not (doubt or alternative or questions):
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

    # Подменяем id-упоминания в текстах на названия целей
    goals_map = {g["id"]: g["title"] for g in db.get_goals()}

    def humanize(s: str) -> str:
        if not s:
            return ""
        for gid, title in goals_map.items():
            s = re.sub(rf"\bцел[ьи]\s*#?\s*{gid}\b", f'«{title}»', s, flags=re.IGNORECASE)
            s = re.sub(rf"\bgoal[_\s]*id\s*=?\s*{gid}\b", f'«{title}»', s, flags=re.IGNORECASE)
            s = re.sub(rf"\bid\s*=?\s*{gid}\b", f'«{title}»', s, flags=re.IGNORECASE)
            s = re.sub(rf"#{gid}\b", f'«{title}»', s)
        return s

    thinking = humanize(thinking)
    alternative = humanize(alternative)
    doubt = humanize(doubt)

    # Формируем красивый ответ
    msg_parts = []

    if updated_tasks:
        STATUS_LABEL = {
            "done": "✅ Закрыто",
            "in_progress": "🟡 Взято в работу",
            "todo": "🔵 Возвращено в очередь",
        }
        msg_parts.append("📌 Обновил статусы:")
        for t in updated_tasks:
            label = STATUS_LABEL.get(t["_new_status"], t["_new_status"])
            msg_parts.append(f"  {label}: {t['title']}")
        msg_parts.append("")

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
                msg_parts.append(f"🎯 К цели «{goal['title']}»")
                if goal.get("metric"):
                    msg_parts.append(f"   📊 {goal['metric']}")
            else:
                msg_parts.append("📝 Без цели:")
            for t in tasks:
                line = f"  ✅ {t['title']}"
                if t.get("deadline"):
                    line += f" (до {t['deadline']})"
                msg_parts.append(line)
                if t.get("context"):
                    ctx = t['context'].split(".")[0]
                    if len(ctx) > 100: ctx = ctx[:100] + "..."
                    msg_parts.append(f"     💭 {ctx}")
        msg_parts.append("")

    if thinking:
        msg_parts.append(f"🧠 {thinking}")

    if questions:
        msg_parts.append("\n❓ Уточни:")
        for q in questions[:4]:
            msg_parts.append(f"  • {q}")

    if alternative:
        msg_parts.append(f"\n💡 Альтернатива: {alternative}")

    if doubt:
        msg_parts.append(f"\n⚠️ Сомнение: {doubt}")

    msg_parts.append(f"\n🔗 {TRACKER_URL}")

    tg_send(chat_id, "\n".join(msg_parts), reply_to=msg_id)


def send_deadline_reminders(chat_id):
    """Проверяет дедлайны и шлёт напоминание сгруппированное по целям."""
    tasks = db.get_tasks()
    goals = db.get_goals()
    goals_map = {g["id"]: g for g in goals}
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

    # Группируем по статусу срочности
    buckets = {"overdue": [], "today": [], "soon": []}
    for t in active:
        dl = parse_dl(t.get("deadline", ""))
        if not dl: continue
        if dl < today: buckets["overdue"].append(t)
        elif dl == today: buckets["today"].append(t)
        elif dl <= today + timedelta(days=3): buckets["soon"].append(t)

    def group_by_goal(tasks_list):
        by_goal = {}
        for t in tasks_list:
            gid = t.get("goal_id")
            by_goal.setdefault(gid, []).append(t)
        return by_goal

    def format_bucket(label_icon, label, tasks_list):
        if not tasks_list:
            return ""
        by_goal = group_by_goal(tasks_list)
        lines = [f"\n{label_icon} *{label}*"]
        for gid, ts in by_goal.items():
            goal = goals_map.get(gid)
            if goal:
                title = goal["title"]
                metric = goal.get("metric")
                lines.append(f"\n🎯 *{title}*" + (f"\n   📊 {metric}" if metric else ""))
            else:
                lines.append("\n📝 *Без цели*")
            for t in ts:
                assignee = t.get("assignee", "—")
                context = (t.get("context") or "").strip()
                deadline = t.get("deadline", "")
                lines.append(f"   • {t['title']}")
                lines.append(f"     👤 {assignee}" + (f"  📅 {deadline}" if deadline else ""))
                if context:
                    # Обрезаем длинный контекст до одного-двух предложений
                    short = context.split(".")[0]
                    if len(short) > 110:
                        short = short[:110] + "..."
                    lines.append(f"     💭 {short}")
        return "\n".join(lines)

    parts = []
    if buckets["overdue"]:
        parts.append(format_bucket("🚨", f"ПРОСРОЧЕНО ({len(buckets['overdue'])})", buckets["overdue"]))
    if buckets["today"]:
        parts.append(format_bucket("⏰", f"Сегодня дедлайн ({len(buckets['today'])})", buckets["today"]))
    if buckets["soon"]:
        parts.append(format_bucket("📅", f"Скоро 3 дня ({len(buckets['soon'])})", buckets["soon"]))

    if parts:
        msg = "📊 *Сводка по дедлайнам*\n" + "".join(parts) + f"\n\n🔗 {TRACKER_URL}"
        tg_send(chat_id, msg)
    else:
        tg_send(chat_id, f"✅ Горящих задач нет — все дедлайны под контролем!\n\n🔗 {TRACKER_URL}")


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.route("/webhook/<path:token>", methods=["POST"])
def webhook(token):
    data = request.json
    if not data:
        return "ok"

    msg = data.get("message") or data.get("channel_post")
    if not msg:
        return "ok"

    chat_id = msg["chat"]["id"]
    msg_id = msg["message_id"]
    user = msg.get("from", {})
    author = (user.get("first_name", "") + " " + user.get("last_name", "")).strip() or user.get("username", "")
    msg_date = datetime.now().strftime("%d %B %Y")

    text = msg.get("text", "")
    voice_source = ""

    # Если это голосовое или видеокружок — распознаём
    if not text:
        voice = msg.get("voice") or msg.get("video_note") or msg.get("audio")
        if voice and voice.get("file_id"):
            tg_send(chat_id, "🎙 Слушаю голосовое...", reply_to=msg_id)
            text = transcribe_voice(voice["file_id"])
            if not text:
                tg_send(chat_id, "❌ Не смог распознать голосовое", reply_to=msg_id)
                return "ok"
            # Telegram держит до 4096 символов в одном сообщении
            preview = text if len(text) <= 3500 else text[:3500] + "\n…(обрезано)"
            tg_send(chat_id, "🎙 Расшифровка:\n" + preview, reply_to=msg_id, markdown=False)

    if not text:
        return "ok"

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

    # Анализ обычного/голосового сообщения
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
