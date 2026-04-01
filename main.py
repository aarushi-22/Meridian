'''
from auth.google_auth import get_credentials
from services.gmail_service import readID
from services.gmail_service import readMessage
from services.gmail_service import getMail

def main():
    creds = get_credentials()
    print("Authentication successful!")
    messages = readID(creds)
    for msg in messages:
        data = readMessage(creds,msg["id"])
        body = getMail(creds,msg["id"])
        

if __name__ == "__main__":
    main()
'''
"""
main.py
Meridian entrypoint.

Usage:
  python main.py setup        — interactive first-run configuration wizard
  python main.py run          — run one poll cycle immediately (Phase 1 testing)
  python main.py server       — start the FastAPI server (Phase 3+)
  python main.py status       — show config and recent events
"""

"""
main.py
Meridian entrypoint.

Usage:
  python main.py setup        — interactive first-run configuration wizard
  python main.py run          — run one poll cycle immediately (Phase 1 testing)
  python main.py server       — start the FastAPI server (Phase 3+)
  python main.py status       — show config and recent events
"""

import sys
import os

# Load .env before anything else so all modules see the environment variables
from dotenv import load_dotenv
load_dotenv()

from db.database import init_db, get_user_config, update_user_config, get_recent_events
from auth.google_auth import get_credentials


def cmd_setup():
    """Interactive setup wizard for first-time configuration."""
    print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  Meridian — First-time Setup")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    init_db()

    # Degree type
    while True:
        degree = input("Your degree type [btech/mtech]: ").strip().lower()
        if degree in ("btech", "mtech"):
            break
        print("  Please enter 'btech' or 'mtech'")

    # Registration number
    reg_no = input("Your registration number (e.g. 21BCE1234): ").strip()

    # Sender IDs
    print("\nEnter the placement cell email IDs to monitor.")
    print("You can add multiple, separated by commas.")
    sender_input = input("Sender email(s): ").strip()
    sender_ids = [s.strip() for s in sender_input.split(",") if s.strip()]

    # Reminder hours
    print("\nWhen to send reminders before the deadline?")
    print("Default: 24 hours and 2 hours before. Press Enter to keep default.")
    reminder_input = input("Reminder hours (comma-separated, e.g. 48,24,2): ").strip()
    if reminder_input:
        try:
            reminder_hours = [int(h.strip()) for h in reminder_input.split(",")]
        except ValueError:
            print("  Invalid input — using defaults [24, 2]")
            reminder_hours = [24, 2]
    else:
        reminder_hours = [24, 2]

    # Calendar
    print("\nWhich Google Calendar to write to?")
    print("Press Enter for your primary calendar, or type a calendar ID.")
    cal_input = input("Calendar ID [primary]: ").strip()
    calendar_id = cal_input if cal_input else "primary"

    # Save
    update_user_config(
        degree_type=degree,
        registration_no=reg_no,
        sender_ids=sender_ids,
        reminder_hours=reminder_hours,
        calendar_id=calendar_id,
    )

    print("\nConfiguration saved.")

    # Only trigger auth if token does not already exist
    if os.path.exists("token.json"):
        print("\n Already authenticated (token.json exists). Skipping sign-in.")
        print("   Delete token.json and re-run setup to switch accounts.\n")
    else:
        print("\nNow opening browser for Google sign-in...")
        try:
            creds = get_credentials()
            print(" Authentication successful!\n")
        except Exception as e:
            print(f" Authentication failed: {e}\n")
            return

    print("Setup complete. Run `python main.py run` to test your first poll cycle.\n")


def cmd_run():
    """Run one poll cycle right now — good for Phase 1 testing."""
    init_db()

    if not os.path.exists("token.json"):
        print("\n❌ Not authenticated yet. Run `python main.py setup` first to sign in.\n")
        sys.exit(1)

    config = get_user_config()
    if not config.get("sender_ids"):
        print("No sender IDs configured. Run `python main.py setup` first.")
        sys.exit(1)

    print(f"\n[Meridian] Running poll cycle...")
    print(f"  Degree filter : {config['degree_type']}")
    print(f"  Watching      : {', '.join(config['sender_ids'])}")
    print(f"  Calendar      : {config['calendar_id']}\n")

    from core.pipeline import run_poll_cycle
    stats = run_poll_cycle()

    print(f"\n━━━━ Result ━━━━")
    print(f"  Processed : {stats['processed']}")
    print(f"  Scheduled : {stats['scheduled']}")
    print(f"  Skipped   : {stats['skipped']}")
    print(f"  Errors    : {stats['errors']}\n")

def cmd_server():
    """Start the FastAPI server."""
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"\n[Meridian] Starting server on http://0.0.0.0:{port}")
    print(f"  Webhook endpoint : POST /webhook/gmail")
    print(f"  Docs             : http://localhost:{port}/docs\n")
    uvicorn.run("api.app:app", host="0.0.0.0", port=port, reload=False)


def cmd_status():
    """Show current config and recent events."""
    init_db()
    config = get_user_config()
    events = get_recent_events(limit=10)

    print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  Meridian — Status")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    print(f"  Degree type    : {config.get('degree_type', 'not set')}")
    print(f"  Reg number     : {'set' if config.get('registration_no') else 'not set'}")
    print(f"  Watching       : {', '.join(config.get('sender_ids', [])) or 'none'}")
    print(f"  Reminders      : {config.get('reminder_hours', [])} hours before")
    print(f"  Calendar       : {config.get('calendar_id', 'primary')}")

    print(f"\n  Recent events ({len(events)}):")
    if events:
        for e in events:
            print(f"    • {e['company_name']} — {e['deadline']} "
                  f"(confidence: {e['confidence']:.0%})")
    else:
        print("    (none yet)")
    print()


COMMANDS = {
    "setup":  cmd_setup,
    "run":    cmd_run,
    "server": cmd_server,
    "status": cmd_status,
}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd not in COMMANDS:
        print(f"Unknown command '{cmd}'. Available: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[cmd]()