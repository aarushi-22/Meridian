# Meridian

> Reads placement cell emails. Schedules internship deadlines to your Google Calendar.

---

## How it works

1. Meridian connects to your Gmail via Google OAuth (read-only)
2. It watches for emails from your placement cell sender ID
3. An AI agent (Claude) parses each email — extracts company name, deadline, and application link
4. It creates a Google Calendar event with reminders before the deadline
5. Every email is fingerprinted so nothing is ever scheduled twice

---

## Project structure

```
meridian/
├── auth/
│   └── google_auth.py          # OAuth 2.0 flow, token refresh
├── services/
│   ├── gmail_service.py        # Gmail API — fetch, decode, parse attachments
│   └── calendar_service.py     # Google Calendar API — create events
├── core/
│   ├── extractor.py            # Claude AI — extract company/deadline/link from email
│   ├── pipeline.py             # Orchestrator — ties all steps together
│   └── scheduler.py            # APScheduler (90-min poll) + Pub/Sub push handler
├── api/
│   └── app.py                  # FastAPI server — webhook + REST endpoints
├── db/
│   └── database.py             # SQLite — idempotency store + user config
├── main.py                     # CLI entrypoint
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Quickstart

### 1. Install dependencies

```bash
cd meridian
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Set up Google Cloud credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project (or use an existing one)
3. Enable **Gmail API** and **Google Calendar API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
5. Application type: **Desktop app**
6. Download the JSON file and save it as `credentials.json` in the project root

### 3. Set your Anthropic API key

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

Then load it before running:
```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or use python-dotenv
```

### 4. First-time setup

```bash
python main.py setup
```

This will ask you for:
- Your degree type (btech / mtech)
- Your registration number
- The placement cell email ID(s) to watch (e.g. `placementcell@vit.ac.in`)
- Reminder timing (default: 24 hrs and 2 hrs before deadline)
- Which Google Calendar to write to

A browser window will open for Google sign-in. After that, `token.json` is saved locally.

### 5. Run your first test

```bash
python main.py run
```

This fetches the 10 most recent emails from your configured senders, runs AI extraction on each, and schedules calendar events for any upcoming deadlines found.

---

## Commands

| Command | What it does |
|---|---|
| `python main.py setup` | First-time interactive configuration wizard |
| `python main.py run` | Run one poll cycle immediately |
| `python main.py status` | View config and recent scheduled events |
| `python main.py server` | Start FastAPI server (for push notifications) |

---

## Phases

### Phase 1 — Local script (works right now)
Run `python main.py run` manually or set up a cron job. No server needed.

```bash
# Example: run every 90 minutes via cron
*/90 * * * * cd /path/to/meridian && python main.py run
```

### Phase 2 — Automatic polling
Start the server and the built-in scheduler handles polling every 90 minutes:
```bash
python main.py server
```

### Phase 3 — Gmail push notifications (instant)
For near-instant email detection, set up Gmail Pub/Sub:

1. Create a Google Cloud Pub/Sub topic
2. Grant `gmail-api-push@system.gserviceaccount.com` the `Pub/Sub Publisher` role on it
3. Set `PUBSUB_TOPIC=projects/YOUR_PROJECT/topics/YOUR_TOPIC` in `.env`
4. Expose your server publicly (e.g. via [ngrok](https://ngrok.com) for dev, or deploy to Railway/Fly.io)
5. Start the server — it auto-registers the Gmail watch on startup

Google will POST to `https://your-server.com/webhook/gmail` whenever a new email arrives.

---

## How idempotency works

Every processed email is stored in SQLite using Gmail's native `message_id` as the primary key.

```
Email arrives
     │
     ▼
Is message_id in processed_messages?
     │
    YES ──→ Skip immediately (no API calls made)
     │
    NO
     │
     ▼
Extract + schedule
     │
     ▼
Write message_id to DB  ← only after calendar write succeeds
```

This means:
- The 90-min poll and push notifications can both fire on the same email — only one event is ever created
- If the calendar write fails, the message is NOT marked processed, so it will be retried next cycle
- Restarting the server never causes duplicate events

---

## How AI extraction works

For each email, the Claude API receives:
- Email subject and date
- Plain-text body (HTML stripped)
- A list of URLs found in the email
- Spreadsheet attachment content (for round 2/3 shortlist emails)

Claude returns structured JSON:
```json
{
  "company_name": "Google",
  "deadline_iso": "2025-02-15T23:59:00+05:30",
  "application_url": "https://careers.google.com/...",
  "degree_target": "btech",
  "confidence": 0.9,
  "is_shortlist": false
}
```

**Important:** The AI only selects application URLs from links literally present in the email — it never generates or hallucinates URLs.

Low-confidence events (confidence < 0.7) get a ⚠ prefix in the calendar title and a note to verify the deadline manually.

---

## Round 2/3 shortlist detection

When your placement cell sends a shortlist with student registration numbers in an Excel attachment:

1. Meridian downloads and parses the `.xlsx` file using `openpyxl`
2. It searches for your registration number in the spreadsheet **in Python** (never sent to the AI)
3. If found, the calendar event is flagged "You appear in the shortlist" with the relevant row

---

## Security

| Practice | Implementation |
|---|---|
| OAuth 2.0 only | Never stores passwords. Uses Google's own consent screen. |
| Minimal scopes | `gmail.readonly` + `calendar.events` — nothing else |
| No email storage | Raw email bodies are never written to disk or database |
| Token stored locally | `token.json` lives only on your machine |
| Registration number | Stored in SQLite, never logged, never sent to external APIs |
| Revocation | `DELETE /revoke` calls Google's revoke endpoint and deletes `token.json` |
| `.gitignore` | `credentials.json`, `token.json`, `.env`, and `*.db` are all gitignored |

---

## API endpoints (server mode)

| Method | Route | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/webhook/gmail` | Gmail Pub/Sub push receiver |
| `GET` | `/events` | List recently scheduled events |
| `GET` | `/config` | View user configuration |
| `POST` | `/config` | Update user configuration |
| `POST` | `/run` | Manually trigger a poll cycle |
| `DELETE` | `/revoke` | Revoke Google access |

Interactive API docs available at `http://localhost:8000/docs` when the server is running.

---

## Troubleshooting

**`credentials.json not found`**
Download your OAuth credentials from Google Cloud Console and place the file in the project root.

**`No sender IDs configured`**
Run `python main.py setup` first.

**`deadline already passed`**
The email was processed but the deadline had already passed. This is expected for old emails on first run.

**Calendar events created with ⚠ prefix**
The AI wasn't confident about the deadline date. Open the original email to verify before applying.

**`PUBSUB_TOPIC not set — polling-only mode active`**
This is fine for Phase 1/2. Push notifications are optional. Polling every 90 minutes will catch all emails.
