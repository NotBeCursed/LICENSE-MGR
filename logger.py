import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import request

DB_PATH = Path(os.environ.get("AUTH_DB_PATH", "users.db"))


def init_logs_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                username  TEXT    NOT NULL,
                ip        TEXT,
                action    TEXT    NOT NULL,
                details   TEXT
            )
        """)
        conn.commit()


def _get_ip() -> str:
    try:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.remote_addr
    except RuntimeError:
        return None


def log_action(username: str, action: str, details: str = None) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ip = _get_ip()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO logs (timestamp, username, ip, action, details) VALUES (?, ?, ?, ?, ?)",
            (ts, username, ip, action, details),
        )
        conn.commit()


def get_logs(limit: int = 1000) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
