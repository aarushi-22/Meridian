"""
db/database.py
SQLite database layer for Meridian.
Tables:
  - processed_messages : idempotency store keyed on Gmail message_id
  - scheduled_events   : maps message_id -> Google Calendar event_id
  - user_config        : single-row user preferences
"""

import sqlite3
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.environ.get("MERIDIAN_DB_PATH", "meridian.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call on every startup."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id      TEXT PRIMARY KEY,
                sender          TEXT NOT NULL,
                subject         TEXT,
                processed_at    TEXT NOT NULL,
                extraction_ok   INTEGER NOT NULL DEFAULT 0,
                skip_reason     TEXT
            );

            CREATE TABLE IF NOT EXISTS scheduled_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id      TEXT NOT NULL REFERENCES processed_messages(message_id),
                calendar_event_id TEXT NOT NULL,
                company_name    TEXT,
                deadline        TEXT,
                application_url TEXT,
                degree_target   TEXT,
                confidence      REAL,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_config (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                degree_type     TEXT NOT NULL DEFAULT 'btech',
                registration_no TEXT,
                sender_ids      TEXT NOT NULL DEFAULT '[]',
                reminder_hours  TEXT NOT NULL DEFAULT '[24, 2]',
                calendar_id     TEXT NOT NULL DEFAULT 'primary',
                updated_at      TEXT NOT NULL
            );
        """)
        # Insert default config row if missing
        conn.execute("""
            INSERT OR IGNORE INTO user_config (id, updated_at)
            VALUES (1, ?)
        """, (datetime.utcnow().isoformat(),))
    print("[db] Initialised meridian.db")


# ── Idempotency ──────────────────────────────────────────────────────────────

def is_processed(message_id: str) -> bool:
    """Return True if this Gmail message_id has already been handled."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?",
            (message_id,)
        ).fetchone()
    return row is not None


def mark_processed(message_id: str, sender: str, subject: str,
                   extraction_ok: bool, skip_reason: Optional[str] = None) -> None:
    """Record a message as handled (called AFTER calendar write succeeds)."""
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO processed_messages
                (message_id, sender, subject, processed_at, extraction_ok, skip_reason)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            message_id, sender, subject,
            datetime.utcnow().isoformat(),
            1 if extraction_ok else 0,
            skip_reason
        ))


def save_event(message_id: str, calendar_event_id: str, company_name: str,
               deadline: str, application_url: str, degree_target: str,
               confidence: float) -> None:
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO scheduled_events
                (message_id, calendar_event_id, company_name, deadline,
                 application_url, degree_target, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message_id, calendar_event_id, company_name, deadline,
            application_url, degree_target, confidence,
            datetime.utcnow().isoformat()
        ))


# ── User config ───────────────────────────────────────────────────────────────

def get_user_config() -> dict:
    import json
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM user_config WHERE id = 1").fetchone()
    if not row:
        return {}
    cfg = dict(row)
    cfg["sender_ids"] = json.loads(cfg["sender_ids"])
    cfg["reminder_hours"] = json.loads(cfg["reminder_hours"])
    return cfg


def update_user_config(**kwargs) -> None:
    import json
    allowed = {"degree_type", "registration_no", "sender_ids",
               "reminder_hours", "calendar_id"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    # Serialize lists to JSON strings
    for k in ("sender_ids", "reminder_hours"):
        if k in updates and isinstance(updates[k], list):
            updates[k] = json.dumps(updates[k])

    updates["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values())
    with get_connection() as conn:
        conn.execute(f"UPDATE user_config SET {cols} WHERE id = 1", vals)


# ── Reporting ─────────────────────────────────────────────────────────────────

def get_recent_events(limit: int = 20) -> list:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT se.*, pm.subject, pm.processed_at
            FROM scheduled_events se
            JOIN processed_messages pm ON se.message_id = pm.message_id
            ORDER BY se.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]