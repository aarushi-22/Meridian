"""
core/pipeline.py
The main processing pipeline for Meridian.

Flow per email:
  1. Check processed_messages table (idempotency guard)
  2. Fetch full message detail from Gmail
  3. Filter by sender ID
  4. Run AI extraction
  5. Check degree eligibility
  6. Create Google Calendar event
  7. Mark message as processed in DB

This module is the single place that calls all other modules together.
"""

from typing import Optional
from auth.google_auth import get_credentials
from services.gmail_service import fetch_recent_messages, fetch_messages_since, get_message_detail
from services.calendar_service import create_deadline_event
from core.extractor import extract_internship_details, should_schedule_for_user
from db.database import is_processed, mark_processed, save_event, get_user_config


def process_single_message(message_id: str, creds, config: dict) -> dict:
    """
    Process one email message end-to-end.
    Returns a result dict describing what happened.
    """
    # ── Step 1: Idempotency check ─────────────────────────────────────────────
    if is_processed(message_id):
        return {"message_id": message_id, "status": "skipped", "reason": "already processed"}

    # ── Step 2: Fetch message detail ──────────────────────────────────────────
    detail = get_message_detail(creds, message_id)
    if not detail:
        return {"message_id": message_id, "status": "error", "reason": "failed to fetch message"}

    subject = detail["subject"]
    sender  = detail["sender"]

    # ── Step 3: Sender filter ─────────────────────────────────────────────────
    sender_ids = config.get("sender_ids", [])
    if sender_ids:
        sender_email = _extract_email_address(sender)
        if not any(sid.lower() in sender_email.lower() for sid in sender_ids):
            mark_processed(message_id, sender, subject,
                           extraction_ok=False, skip_reason="sender not in whitelist")
            return {"message_id": message_id, "status": "skipped", "reason": "sender not whitelisted"}

    print(f"\n[pipeline] Processing: {subject}")
    print(f"           From: {sender}")

    # ── Step 4: AI extraction ─────────────────────────────────────────────────
    extracted = extract_internship_details(
        email_body=detail["body"],
        email_subject=subject,
        email_date=detail["received_at"],
        urls_in_email=detail["urls"],
        attachment_texts=detail["attachments"],
        registration_no=config.get("registration_no"),
    )

    print(f"[pipeline] Extracted: company={extracted['company_name']}, "
          f"deadline={extracted['deadline_iso']}, confidence={extracted['confidence']:.2f}")

    # ── Step 5: Eligibility check ─────────────────────────────────────────────
    user_degree = config.get("degree_type", "btech")
    should, reason = should_schedule_for_user(extracted, user_degree)

    if not should:
        mark_processed(message_id, sender, subject,
                       extraction_ok=False, skip_reason=reason)
        print(f"[pipeline] Skipping event: {reason}")
        return {"message_id": message_id, "status": "skipped", "reason": reason}

    # ── Step 6: Create calendar event ─────────────────────────────────────────
    event_id = create_deadline_event(
        creds=creds,
        company_name=extracted["company_name"],
        deadline_dt=extracted["deadline_dt"],
        application_url=extracted.get("application_url", ""),
        email_subject=subject,
        email_sender=sender,
        degree_target=extracted["degree_target"],
        confidence=extracted["confidence"],
        reminder_hours=config.get("reminder_hours", [24, 2]),
        calendar_id=config.get("calendar_id", "primary"),
        registration_no=config.get("registration_no"),
        round_info=extracted.get("round_info"),
    )

    if not event_id:
        # Don't mark processed — we want to retry next cycle
        return {"message_id": message_id, "status": "error", "reason": "calendar write failed"}

    # ── Step 7: Record in DB (only after successful calendar write) ───────────
    mark_processed(message_id, sender, subject, extraction_ok=True)
    save_event(
        message_id=message_id,
        calendar_event_id=event_id,
        company_name=extracted["company_name"],
        deadline=extracted["deadline_iso"],
        application_url=extracted.get("application_url", ""),
        degree_target=extracted["degree_target"],
        confidence=extracted["confidence"],
    )

    return {
        "message_id":   message_id,
        "status":       "scheduled",
        "company":      extracted["company_name"],
        "deadline":     extracted["deadline_iso"],
        "event_id":     event_id,
        "confidence":   extracted["confidence"],
    }


def run_poll_cycle(history_id: Optional[str] = None) -> dict:
    """
    Run a full polling cycle:
      - Fetch messages (incremental if history_id provided, else recent 50)
      - Process each one through the pipeline
      - Return summary stats

    This is called by the scheduler every 1.5 hours as a fallback.
    """
    creds  = get_credentials()
    config = get_user_config()

    if not config.get("sender_ids"):
        print("[pipeline] No sender IDs configured. Run `python main.py setup` first.")
        return {"processed": 0, "scheduled": 0, "skipped": 0, "errors": 0}

    # Fetch message stubs
    if history_id:
        from services.gmail_service import fetch_messages_since
        messages = fetch_messages_since(creds, config["sender_ids"], history_id)
    else:
        messages = fetch_recent_messages(creds, config["sender_ids"], max_results=10)

    print(f"[pipeline] Poll cycle: {len(messages)} messages to check")

    stats = {"processed": 0, "scheduled": 0, "skipped": 0, "errors": 0}

    for msg in messages:
        result = process_single_message(msg["id"], creds, config)
        stats["processed"] += 1
        if result["status"] == "scheduled":
            stats["scheduled"] += 1
        elif result["status"] == "skipped":
            stats["skipped"] += 1
        elif result["status"] == "error":
            stats["errors"] += 1

    print(f"[pipeline] Done. scheduled={stats['scheduled']}, "
          f"skipped={stats['skipped']}, errors={stats['errors']}")
    return stats


def _extract_email_address(sender_str: str) -> str:
    """Extract bare email from 'Display Name <email@domain.com>' format."""
    import re
    match = re.search(r'<([^>]+)>', sender_str)
    return match.group(1) if match else sender_str