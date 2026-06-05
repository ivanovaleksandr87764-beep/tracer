"""Supabase storage — заменяет backlog.json."""
import os
import json
import httpx
from pathlib import Path

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
BACKLOG_FILE = Path(__file__).parent / "backlog.json"

# Используем Supabase если есть переменные, иначе — локальный JSON
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


# ── Supabase ──────────────────────────────────────────────────────────────────

def _sb_get(params: dict = None) -> list[dict]:
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS, params=params or {"order": "id.asc"})
    return r.json() if r.status_code == 200 else []


def _sb_insert(task: dict) -> dict | None:
    task.pop("id", None)
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS, json=task)
    data = r.json()
    return data[0] if isinstance(data, list) and data else None


def _sb_update(task_id: int, data: dict) -> dict | None:
    r = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/tasks",
        headers=HEADERS,
        params={"id": f"eq.{task_id}"},
        json=data,
    )
    result = r.json()
    return result[0] if isinstance(result, list) and result else None


def _sb_delete(task_id: int) -> bool:
    r = httpx.delete(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS, params={"id": f"eq.{task_id}"})
    return r.status_code in (200, 204)


# ── Local JSON fallback ────────────────────────────────────────────────────────

def _local_load() -> list[dict]:
    if BACKLOG_FILE.exists():
        with open(BACKLOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def _local_save(tasks: list[dict]):
    with open(BACKLOG_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def get_tasks(filters: dict = None) -> list[dict]:
    if USE_SUPABASE:
        params = {"order": "id.asc"}
        if filters:
            for k, v in filters.items():
                params[k] = f"eq.{v}"
        return _sb_get(params)
    tasks = _local_load()
    if filters:
        for k, v in filters.items():
            tasks = [t for t in tasks if t.get(k) == v]
    return tasks


def create_task(data: dict) -> dict:
    if USE_SUPABASE:
        return _sb_insert(data) or data

    tasks = _local_load()
    next_id = max((t.get("id", 0) for t in tasks), default=0) + 1
    data["id"] = next_id
    tasks.append(data)
    _local_save(tasks)
    return data


def update_task(task_id: int, data: dict) -> dict | None:
    if USE_SUPABASE:
        return _sb_update(task_id, data)

    tasks = _local_load()
    for t in tasks:
        if t.get("id") == task_id:
            t.update(data)
            _local_save(tasks)
            return t
    return None


def delete_task(task_id: int) -> bool:
    if USE_SUPABASE:
        return _sb_delete(task_id)

    tasks = _local_load()
    new = [t for t in tasks if t.get("id") != task_id]
    _local_save(new)
    return True


def get_stats() -> dict:
    tasks = get_tasks()
    return {
        "total": len(tasks),
        "todo": sum(1 for t in tasks if t.get("status") == "todo"),
        "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
        "done": sum(1 for t in tasks if t.get("status") == "done"),
        "high": sum(1 for t in tasks if t.get("priority") == "high"),
    }


def migrate_from_json():
    """Одноразовый перенос данных из backlog.json в Supabase."""
    if not USE_SUPABASE or not BACKLOG_FILE.exists():
        return
    tasks = _local_load()
    if not tasks:
        return
    for task in tasks:
        task.pop("id", None)
        httpx.post(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS, json=task)
    print(f"Migrated {len(tasks)} tasks to Supabase")
