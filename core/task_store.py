"""Small durable task store. Each operation owns its SQLite connection."""
import json
import sqlite3
from pathlib import Path
from datetime import datetime


class TaskStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, filename TEXT, status TEXT, created_at TEXT,
                steps TEXT, result TEXT, error TEXT, params TEXT)''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def create(self, task):
        with self.connect() as db:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                (task.task_id, task.filename, task.status, task.created_at, '[]', None, None, json.dumps(task.params)))

    def update(self, task):
        with self.connect() as db:
            db.execute('UPDATE tasks SET status=?, steps=?, result=?, error=? WHERE task_id=?',
                (task.status, json.dumps(task.steps, ensure_ascii=False),
                 json.dumps(task.result, ensure_ascii=False) if task.result is not None else None,
                 task.error, task.task_id))

    def get(self, task_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        for key in ('steps', 'result', 'params'):
            data[key] = json.loads(data[key]) if data[key] is not None else None
        return data

    def interrupt_unfinished(self):
        with self.connect() as db:
            db.execute("UPDATE tasks SET status='interrupted', error='服务重启，原任务未完成；请重新提交合同。' WHERE status IN ('pending','running')")
