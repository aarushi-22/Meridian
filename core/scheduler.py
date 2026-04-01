"""
core/scheduler.py
Two ingestion mechanisms for Meridian:

1. Gmail Pub/Sub push  — Google calls POST /webhook/gmail when a new email arrives.
   Near-instant. Requires a public HTTPS server.

2. Polling fallback    — APScheduler runs run_poll_cycle() every 90 minutes.
   Catches anything the push mechanism missed (e.g. watch token expiry).

The Pub/Sub watch token expires every 7 days — we auto-renew it on startup
and via a daily cron job.
"""

import os
import json
import base64
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from core.pipeline import run_poll_cycle, process_single_message
from auth.google_auth import get_credentials
from db.database import get_user_config

# In-memory store for the current Gmail history ID
# (persisted to DB in production — sufficient as module-level for Phase 1)
_current_history_id: Optional[str] = None


# ── Gmail Pub/Sub Watch ───────────────────────────────────────────────────────

PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "")   # e.g. projects/myproject/topics/meridian

def setup_gmail_watch(creds) -> Optional[str]:
    """
    Register a Gmail push watch on the inbox.
    Returns the expiration timestamp string, or None on failure.
    Call this on startup and re-call every 6 days.
    """
    if not PUBSUB_TOPIC:
        print("[scheduler] PUBSUB_TOPIC not set — skipping Gmail watch setup. "
              "Polling-only mode active.")
        return None

    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    service = build("gmail", "v1", credentials=creds)
    try:
        result = service.users().watch(
            userId="me",
            body={
                "topicName": PUBSUB_TOPIC,
                "labelIds": ["INBOX"],
            }
        ).execute()
        expiry = result.get("expiration")
        history_id = result.get("historyId")
        _set_history_id(history_id)
        print(f"[scheduler] Gmail watch active. Expires: {expiry}, historyId: {history_id}")
        return expiry
    except HttpError as e:
        print(f"[scheduler] Failed to set up Gmail watch: {e}")
        return None


def _set_history_id(hid: Optional[str]) -> None:
    global _current_history_id
    if hid:
        _current_history_id = hid


# ── Push notification handler (called by FastAPI route) ───────────────────────

def handle_pubsub_push(raw_body: bytes) -> dict:
    """
    Decode a Google Pub/Sub push notification and process the new email.
    
    Pub/Sub sends:
      { "message": { "data": "<base64 JSON>", "messageId": "...", ... } }
    
    The decoded data contains: { "emailAddress": "...", "historyId": "..." }
    """
    try:
        envelope = json.loads(raw_body)
        data_b64  = envelope.get("message", {}).get("data", "")
        data      = json.loads(base64.b64decode(data_b64).decode("utf-8"))
        new_history_id = data.get("historyId")
    except Exception as e:
        print(f"[scheduler] Failed to decode Pub/Sub message: {e}")
        return {"status": "error", "reason": "decode failed"}

    print(f"[scheduler] Push received. New historyId: {new_history_id}")

    creds  = get_credentials()
    config = get_user_config()

    if not config.get("sender_ids"):
        return {"status": "skipped", "reason": "no sender IDs configured"}

    # Fetch new messages since last known history ID
    from services.gmail_service import fetch_messages_since
    old_hid = _current_history_id
    messages = fetch_messages_since(creds, config["sender_ids"], old_hid) if old_hid else []

    _set_history_id(new_history_id)

    results = []
    for msg in messages:
        result = process_single_message(msg["id"], creds, config)
        results.append(result)

    return {"status": "ok", "processed": len(results), "results": results}


# ── Background Scheduler (polling fallback) ───────────────────────────────────

_scheduler: Optional[BackgroundScheduler] = None


def start_scheduler() -> None:
    """
    Start the APScheduler background scheduler.
    - Poll every 90 minutes (fallback)
    - Renew Gmail watch every 6 days
    """
    global _scheduler
    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    # Fallback poll every 90 minutes
    _scheduler.add_job(
        func=_poll_job,
        trigger=IntervalTrigger(minutes=90),
        id="poll_fallback",
        name="Meridian 90-min poll",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Renew Gmail watch every 6 days at 06:00
    _scheduler.add_job(
        func=_renew_watch_job,
        trigger=CronTrigger(day="*/6", hour=6, minute=0),
        id="watch_renew",
        name="Gmail watch renewal",
        replace_existing=True,
    )

    _scheduler.start()
    print("[scheduler] Background scheduler started (90-min poll + watch renewal)")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        print("[scheduler] Scheduler stopped")


def _poll_job() -> None:
    """Job function called by APScheduler."""
    print(f"[scheduler] Running fallback poll at {datetime.now().strftime('%H:%M:%S')}")
    run_poll_cycle(history_id=_current_history_id)


def _renew_watch_job() -> None:
    """Job function to renew Gmail Pub/Sub watch."""
    creds = get_credentials()
    setup_gmail_watch(creds)