import json
import os
import sqlite3
import time

from . import config

os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
_db = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
_db.execute("PRAGMA journal_mode=WAL")
_db.executescript(
    """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, principal TEXT NOT NULL, state TEXT NOT NULL,
  body TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS tasks_by_principal ON tasks(principal, created);
CREATE TABLE IF NOT EXISTS messages(
  principal TEXT NOT NULL, message_id TEXT NOT NULL, hash TEXT NOT NULL,
  task_id TEXT NOT NULL, PRIMARY KEY(principal, message_id));
CREATE TABLE IF NOT EXISTS decisions(key TEXT PRIMARY KEY, body TEXT NOT NULL);
"""
)


def get_task(principal, task_id):
    r = _db.execute("SELECT body FROM tasks WHERE id=? AND principal=?", (task_id, principal)).fetchone()
    return json.loads(r[0]) if r else None


def save_task(principal, t):
    _db.execute(
        "INSERT INTO tasks(id,principal,state,body,created) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET state=excluded.state, body=excluded.body",
        (t["id"], principal, t["status"]["state"], json.dumps(t, ensure_ascii=False), time.time()),
    )


def list_tasks(principal, limit=60):
    rows = _db.execute(
        "SELECT body FROM tasks WHERE principal=? ORDER BY created DESC, rowid DESC LIMIT ?",
        (principal, limit),
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def get_message(principal, mid):
    r = _db.execute("SELECT hash, task_id FROM messages WHERE principal=? AND message_id=?", (principal, mid)).fetchone()
    return {"hash": r[0], "task_id": r[1]} if r else None


def put_message(principal, mid, h, task_id):
    _db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?)", (principal, mid, h, task_id))


def delete_task(principal, task_id, mid):
    _db.execute("DELETE FROM tasks WHERE id=? AND principal=?", (task_id, principal))
    _db.execute("DELETE FROM messages WHERE principal=? AND message_id=?", (principal, mid))


def commit_step(principal, t, mid, h):
    _db.execute("BEGIN IMMEDIATE")
    try:
        save_task(principal, t)
        put_message(principal, mid, h, t["id"])
        _db.execute("COMMIT")
    except Exception:
        _db.execute("ROLLBACK")
        raise


def get_decision(key):
    r = _db.execute("SELECT body FROM decisions WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def put_decision(key, d):
    _db.execute("INSERT OR REPLACE INTO decisions VALUES(?,?)", (key, json.dumps(d, ensure_ascii=False)))


def dump_batch(batch):
    try:
        path = os.path.join(os.path.dirname(config.DB_PATH) or ".", "last_batch.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(batch, f, ensure_ascii=False, indent=1)
    except Exception:
        pass