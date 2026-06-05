"""Flask web app for task tracker backlog."""
import json
import os
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify
from analyze_tasks import load_backlog, save_backlog, analyze_new_message

app = Flask(__name__)
BACKLOG_FILE = Path(__file__).parent / "backlog.json"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/tasks")
def get_tasks():
    tasks = load_backlog()
    # Filtering
    status = request.args.get("status")
    assignee = request.args.get("assignee")
    priority = request.args.get("priority")
    search = request.args.get("search", "").lower()

    category = request.args.get("category")
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if assignee:
        tasks = [t for t in tasks if t.get("assignee") == assignee]
    if priority:
        tasks = [t for t in tasks if t.get("priority") == priority]
    if category:
        tasks = [t for t in tasks if t.get("category") == category]
    if search:
        tasks = [t for t in tasks if search in t.get("title", "").lower() or search in t.get("context", "").lower()]

    return jsonify(tasks)


@app.route("/api/tasks/<int:task_id>", methods=["PATCH"])
def update_task(task_id):
    tasks = load_backlog()
    data = request.json
    for t in tasks:
        if t.get("id") == task_id:
            t.update(data)
            save_backlog(tasks)
            return jsonify(t)
    return jsonify({"error": "not found"}), 404


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    tasks = load_backlog()
    tasks = [t for t in tasks if t.get("id") != task_id]
    save_backlog(tasks)
    return jsonify({"ok": True})


@app.route("/api/tasks", methods=["POST"])
def create_task():
    tasks = load_backlog()
    data = request.json
    next_id = max((t.get("id", 0) for t in tasks), default=0) + 1
    task = {
        "id": next_id,
        "title": data.get("title", ""),
        "assignee": data.get("assignee", ""),
        "deadline": data.get("deadline"),
        "priority": data.get("priority", "medium"),
        "status": data.get("status", "todo"),
        "source_date": datetime.now().strftime("%d %B %Y"),
        "context": data.get("context", "Добавлено вручную"),
    }
    tasks.append(task)
    save_backlog(tasks)
    return jsonify(task)


@app.route("/api/analyze", methods=["POST"])
def analyze_message():
    """Analyze a new chat message and extract tasks."""
    data = request.json
    message = {
        "id": "manual",
        "date": datetime.now().strftime("%d %B %Y"),
        "time": datetime.now().strftime("%H:%M"),
        "author": data.get("author", ""),
        "text": data.get("text", ""),
    }
    new_tasks = analyze_new_message(message)
    return jsonify(new_tasks)


@app.route("/api/assignees")
def get_assignees():
    tasks = load_backlog()
    assignees = sorted({t.get("assignee", "") for t in tasks if t.get("assignee")})
    return jsonify(assignees)


@app.route("/api/stats")
def get_stats():
    tasks = load_backlog()
    stats = {
        "total": len(tasks),
        "todo": sum(1 for t in tasks if t.get("status") == "todo"),
        "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
        "done": sum(1 for t in tasks if t.get("status") == "done"),
        "high": sum(1 for t in tasks if t.get("priority") == "high"),
    }
    return jsonify(stats)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
