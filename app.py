"""Flask web app for task tracker backlog."""
import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import db
from analyze_tasks import analyze_new_message

app = Flask(__name__)


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
    if result:
        return jsonify(result)
    return jsonify({"error": "not found"}), 404


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
    message = {
        "id": "manual",
        "date": datetime.now().strftime("%d %B %Y"),
        "time": datetime.now().strftime("%H:%M"),
        "author": data.get("author", ""),
        "text": data.get("text", ""),
    }
    new_tasks = analyze_new_message(message, save_fn=db.create_task)
    return jsonify(new_tasks)


@app.route("/api/assignees")
def get_assignees():
    tasks = db.get_tasks()
    assignees = sorted({t.get("assignee", "") for t in tasks if t.get("assignee")})
    return jsonify(assignees)


@app.route("/api/stats")
def get_stats():
    return jsonify(db.get_stats())


@app.route("/api/migrate", methods=["POST"])
def migrate():
    """Перенести backlog.json в Supabase — запустить один раз."""
    db.migrate_from_json()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
