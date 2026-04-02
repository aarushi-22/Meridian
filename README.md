# Meridian

Automatically extracts internship deadlines from placement emails and schedules them to Google Calendar.

Meridian connects to your Gmail account, detects placement cell emails, extracts internship application deadlines using an LLM, and creates calendar reminders so you never miss an application window.

---

## The problem

During internship season, placement cells send a constant stream of emails — application windows, online tests, shortlist announcements. Miss the email, miss the deadline. Meridian fixes that.

---

## What it does

1. Connects to Gmail using Google OAuth (read-only access)
2. Fetches emails from configured placement cell sender IDs
3. Uses Claude AI to extract structured information — company name, deadline, application link
4. Filters by your degree type and checks shortlist attachments for your registration number
5. Creates a Google Calendar event with configurable reminders
6. Uses Gmail message IDs to ensure each email is processed only once

---

## Architecture

```
Gmail API
   │
   ▼
Meridian Pipeline
   │
   ├── Email parsing + attachment extraction
   ├── AI extraction (Claude Sonnet)
   ├── Degree + shortlist filtering
   ├── Deadline validation
   │
   ▼
Google Calendar API
   │
   ▼
SQLite (idempotency store + event log)
```

---

## Project structure

```
meridian/
├── auth/
│   └── google_auth.py          # OAuth 2.0 flow and token refresh
├── services/
│   ├── gmail_service.py        # Gmail API — fetch, decode, parse attachments
│   └── calendar_service.py     # Google Calendar event creation
├── core/
│   ├── extractor.py            # AI extraction + shortlist detection
│   ├── pipeline.py             # Main orchestration pipeline
│   └── scheduler.py            # 90-min polling + Pub/Sub push handler
├── api/
│   ├── app.py                  # FastAPI server + REST endpoints
│   └── security.py             # CORS, CSRF, rate limiting, OIDC verification
├── db/
│   └── database.py             # SQLite — idempotency store + config + event log
├── main.py                     # CLI entrypoint
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/meridian.git
cd meridian
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate
```

Windows:

```bash
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

## Google API setup

1. Open the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project
3. Enable:
   - Gmail API
   - Google Calendar API
4. Go to **APIs & Services → Credentials**
5. Create **OAuth 2.0 Client ID**
6. Select **Desktop App**
7. Download the credentials file and save it as `credentials.json` in the project root

---

## Environment configuration

```bash
cp .env.example .env
```

Open `.env` and add your Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Get your key at [console.anthropic.com](https://console.anthropic.com). Add $5 credits under Plans & Billing — at roughly $0.002 per email, this lasts months.

---

## First-time setup

```bash
python main.py setup
```

The wizard will ask for:

- Degree type (btech / mtech)
- Registration number
- Placement cell sender email IDs to watch
- Reminder timings (default: 24 hrs and 2 hrs before deadline)
- Target Google Calendar

After configuration, a browser window will open for Google sign-in. Your OAuth token is saved locally as `token.json` and refreshes automatically.

---

## Running Meridian

```bash
python main.py run
```

This will fetch recent placement emails, extract deadlines, and create calendar events. Check your Google Calendar after it completes.

---

## CLI commands

| Command | Description |
|---|---|
| `python main.py setup` | First-time configuration wizard + Google sign-in |
| `python main.py run` | Run one email processing cycle |
| `python main.py status` | Show configuration and recently scheduled events |
| `python main.py server` | Start the FastAPI server |

---

## Automating runs

To run Meridian automatically every 90 minutes on Windows, open PowerShell as administrator and run:

```powershell
$action = New-ScheduledTaskAction -Execute "python" -Argument "main.py run" -WorkingDirectory "C:\path\to\meridian"
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 90) -Once -At (Get-Date)
Register-ScheduledTask -TaskName "Meridian" -Action $action -Trigger $trigger -RunLevel Highest
```

On Linux/macOS, add to crontab:

```bash
*/90 * * * * cd /path/to/meridian && python main.py run
```

---

## AI extraction

For each email, Meridian sends the following to Claude:

- Email subject and date
- Plain text body (HTML stripped)
- Extracted URLs from the email
- Spreadsheet attachment content (if present)

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

The application URL is always chosen from links literally present in the email — Claude cannot generate or hallucinate URLs. Low-confidence extractions are marked with a ⚠ prefix in the calendar event title.

---

## Idempotent processing

Each processed email is recorded in SQLite using Gmail's native `message_id` as the primary key.

```
Email arrives
     │
     ▼
Check message_id in DB
     │
  Exists → Skip immediately
     │
  New
     │
     ▼
Extract + create calendar event
     │
     ▼
Save message_id to DB
```

The DB write only happens after a successful calendar write. If the calendar API fails, the email is not marked processed and will be retried on the next run.

---

## Shortlist detection

For round 2/3 emails containing shortlist spreadsheets:

1. Meridian downloads `.xlsx` attachments
2. Parses them locally using `openpyxl`
3. Searches for your registration number in the spreadsheet
4. If found — schedules the event and flags it as shortlisted
5. If not found — skips the event entirely

Your registration number is **never sent to the AI model**.

---

## Filtering logic

| Email type | Result |
|---|---|
| Round 1 — correct degree, future deadline | Scheduled |
| Round 1 — wrong degree (e.g. M.Tech only) | Skipped |
| Round 1 — deadline already passed | Skipped |
| Round 2/3 — your reg number found in attachment | Scheduled, flagged as shortlisted |
| Round 2/3 — your reg number not found | Skipped |
| Low confidence deadline | Scheduled with ⚠ warning in title |

---

## Security

- **OAuth 2.0 only** — your Gmail password is never seen or stored
- **Minimal scopes** — `gmail.readonly` (cannot send or delete) + `calendar.events`
- **No email storage** — email bodies are never written to disk or database
- **Idempotency** — duplicate events are structurally impossible
- **Prompt injection protection** — email content is sandboxed from AI instructions, malicious patterns are detected before the API call
- **Rate limiting** — per-IP token bucket on all endpoints
- **CSRF protection** — double-submit cookie pattern on all mutating endpoints
- **Security headers** — CSP, X-Frame-Options, HSTS on every response
- **OIDC verification** — Pub/Sub webhook requests verified against Google's public keys

Gitignored files:

```
credentials.json
token.json
.env
*.db
```

---

## API endpoints (server mode)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service health check |
| `GET` | `/csrf-token` | Issue a CSRF token |
| `POST` | `/webhook/gmail` | Gmail Pub/Sub push receiver |
| `GET` | `/events` | List recently scheduled events |
| `GET` | `/config` | View configuration |
| `POST` | `/config` | Update configuration |
| `POST` | `/run` | Trigger manual pipeline run |
| `DELETE` | `/revoke` | Revoke Google OAuth access |

Interactive docs available at `http://localhost:8000/docs`.

---

## Troubleshooting

**`credentials.json` not found**
Download your OAuth credentials from Google Cloud Console and place the file in the project root.

**No sender IDs configured**
Run `python main.py setup`.

**Not authenticated**
Delete `token.json` if it exists and run `python main.py setup` again.

**Calendar 404 error**
You entered your email address as the calendar ID. Use `primary` for your main calendar, or share the target calendar with your authenticated account and use its calendar ID from Google Calendar settings.

**Deadline already passed**
The email was processed successfully but the deadline had already expired. Expected for old emails on first run.

**Low-confidence event warnings**
The AI was uncertain about the deadline date. Verify against the original email before applying.

---

## Roadmap

- [ ] Multi-user support (PostgreSQL + per-user encrypted token storage)
- [ ] Web frontend (setup UI + event dashboard)
- [ ] Gmail Pub/Sub push notifications (real-time instead of polling)
- [ ] PDF attachment support
- [ ] Telegram / WhatsApp notification fallback

---

## Built with

- [Python](https://python.org) + [FastAPI](https://fastapi.tiangolo.com)
- [Gmail API](https://developers.google.com/gmail/api) + [Google Calendar API](https://developers.google.com/calendar)
- [Anthropic Claude API](https://anthropic.com) — `claude-sonnet-4-20250514`
- [SQLite](https://sqlite.org) for local data storage
- [APScheduler](https://apscheduler.readthedocs.io) for background polling

---

## Author

Developed as a personal automation tool for managing internship application deadlines from placement cell emails.
