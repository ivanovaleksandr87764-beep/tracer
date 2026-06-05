"""Supabase storage — tasks + goals."""
import os, json, httpx
from pathlib import Path

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
BACKLOG_FILE = Path(__file__).parent / "backlog.json"
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


# ── Goals ─────────────────────────────────────────────────────────────────────

def get_goals() -> list[dict]:
    if not USE_SUPABASE:
        return []
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/goals", headers=HEADERS,
                  params={"order": "id.asc"}, timeout=10)
    return r.json() if r.status_code == 200 else []


def create_goal(data: dict) -> dict:
    if not USE_SUPABASE:
        return data
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/goals", headers=HEADERS,
                   json=data, timeout=10)
    result = r.json()
    return result[0] if isinstance(result, list) and result else data


def update_goal(goal_id: int, data: dict) -> dict | None:
    if not USE_SUPABASE:
        return None
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/goals", headers=HEADERS,
                    params={"id": f"eq.{goal_id}"}, json=data, timeout=10)
    result = r.json()
    return result[0] if isinstance(result, list) and result else None


def delete_goal(goal_id: int):
    if not USE_SUPABASE:
        return
    # Открепляем задачи от цели
    httpx.patch(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                params={"goal_id": f"eq.{goal_id}"}, json={"goal_id": None}, timeout=10)
    httpx.delete(f"{SUPABASE_URL}/rest/v1/goals", headers=HEADERS,
                 params={"id": f"eq.{goal_id}"}, timeout=10)


def get_goals_with_tasks() -> list[dict]:
    """Возвращает цели со вложенными задачами."""
    goals = get_goals()
    all_tasks = get_tasks()

    task_by_goal: dict[int, list] = {}
    orphans = []
    for t in all_tasks:
        gid = t.get("goal_id")
        if gid:
            task_by_goal.setdefault(gid, []).append(t)
        else:
            orphans.append(t)

    result = []
    for g in goals:
        g["tasks"] = task_by_goal.get(g["id"], [])
        # Считаем прогресс
        tasks = g["tasks"]
        done = sum(1 for t in tasks if t.get("status") == "done")
        g["progress"] = {"total": len(tasks), "done": done,
                         "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
                         "todo": sum(1 for t in tasks if t.get("status") == "todo")}
        result.append(g)

    # Задачи без цели → в отдельный "блок"
    if orphans:
        result.append({
            "id": None,
            "title": "Без цели",
            "metric": None,
            "status": "active",
            "tasks": orphans,
            "progress": {"total": len(orphans), "done": 0, "in_progress": 0, "todo": len(orphans)},
        })

    return result


def get_goal_stats() -> dict:
    goals = get_goals()
    tasks = get_tasks()
    active = sum(1 for g in goals if g.get("status") == "active")
    done_goals = sum(1 for g in goals if g.get("status") == "done")
    done_tasks = sum(1 for t in tasks if t.get("status") == "done")
    total_tasks = len(tasks)
    return {
        "goals_total": len(goals),
        "goals_active": active,
        "goals_done": done_goals,
        "tasks_total": total_tasks,
        "tasks_done": done_tasks,
        "tasks_todo": sum(1 for t in tasks if t.get("status") == "todo"),
        "tasks_in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
    }


# ── Tasks ─────────────────────────────────────────────────────────────────────

def _local_load() -> list[dict]:
    if BACKLOG_FILE.exists():
        with open(BACKLOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def _local_save(tasks: list[dict]):
    with open(BACKLOG_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def get_tasks(filters: dict = None) -> list[dict]:
    if USE_SUPABASE:
        params = {"order": "id.asc"}
        if filters:
            for k, v in filters.items():
                params[k] = f"eq.{v}"
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                      params=params, timeout=10)
        return r.json() if r.status_code == 200 else []
    tasks = _local_load()
    if filters:
        for k, v in filters.items():
            tasks = [t for t in tasks if t.get(k) == v]
    return tasks


def create_task(data: dict) -> dict:
    if USE_SUPABASE:
        data.pop("id", None)
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                       json=data, timeout=10)
        result = r.json()
        return result[0] if isinstance(result, list) and result else data
    tasks = _local_load()
    next_id = max((t.get("id", 0) for t in tasks), default=0) + 1
    data["id"] = next_id
    tasks.append(data)
    _local_save(tasks)
    return data


def update_task(task_id: int, data: dict) -> dict | None:
    if USE_SUPABASE:
        r = httpx.patch(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                        params={"id": f"eq.{task_id}"}, json=data, timeout=10)
        result = r.json()
        return result[0] if isinstance(result, list) and result else None
    tasks = _local_load()
    for t in tasks:
        if t.get("id") == task_id:
            t.update(data)
            _local_save(tasks)
            return t
    return None


def delete_task(task_id: int):
    if USE_SUPABASE:
        httpx.delete(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                     params={"id": f"eq.{task_id}"}, timeout=10)
        return
    tasks = _local_load()
    _local_save([t for t in tasks if t.get("id") != task_id])


def get_stats() -> dict:
    tasks = get_tasks()
    return {
        "total": len(tasks),
        "todo": sum(1 for t in tasks if t.get("status") == "todo"),
        "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
        "done": sum(1 for t in tasks if t.get("status") == "done"),
        "high": sum(1 for t in tasks if t.get("priority") == "high" and t.get("status") != "done"),
    }


def migrate_from_json():
    if not USE_SUPABASE or not BACKLOG_FILE.exists():
        return
    tasks = _local_load()
    for task in tasks:
        task.pop("id", None)
        httpx.post(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
                   json=task, timeout=10)
