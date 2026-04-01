# Meridian

> Automatically extracts internship deadlines from placement emails and schedules them to Google Calendar.

Meridian connects to your Gmail account, detects placement cell emails, extracts internship application deadlines using an LLM, and creates calendar reminders so you never miss an application window.

---

## Overview

Placement cell emails often contain important internship deadlines buried inside long messages or attachments. Meridian automates the process of extracting those deadlines and scheduling them directly into your calendar.

Meridian performs the following steps:

1. Connects to Gmail using Google OAuth (read-only access)
2. Fetches emails from configured placement cell sender IDs
3. Uses an LLM to extract structured information (company, deadline, application link)
4. Creates a Google Calendar event with configurable reminders
5. Uses Gmail message IDs to ensure each email is processed only once

---

## Architecture

```
Gmail API
   │
   ▼
Meridian Pipeline
   │
   ├── Email parsing
   ├── LLM extraction (Claude)
   ├── Deadline validation
   │
   ▼
Google Calendar API
```

---

## Project Structure

```
meridian/
├── auth/
│   └── google_auth.py          # OAuth 2.0 flow and token refresh
├── services/
│   ├── gmail_service.py        # Gmail API integration
│   └── calendar_service.py     # Google Calendar event creation
├── core/
│   ├── extractor.py            # LLM extraction logic
│   ├── pipeline.py             # Main orchestration pipeline
│   └── scheduler.py            # Scheduled polling
├── api/
│   └── app.py                  # FastAPI server for manual triggers
├── db/
│   └── database.py             # SQLite storage for processed emails
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

## Google API Setup

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

## Environment Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Add your API key:

```
ANTHROPIC_API_KEY=your_api_key_here
```

Meridian uses **Claude Haiku** for fast and low-cost email extraction.

---

## First-Time Setup

Run the setup wizard:

```bash
python main.py setup
```

The wizard will ask for:

- Degree type (btech / mtech)
- Registration number
- Placement cell sender email
- Reminder timings
- Target Google Calendar

After configuration, a browser window will open for Google authentication. OAuth tokens are stored locally as `token.json`.

---

## Running Meridian

Run a single email processing cycle:

```bash
python main.py run
```

This will:

1. Fetch recent placement emails
2. Extract internship deadlines
3. Create calendar reminders

---

## CLI Commands

| Command                 | Description                              |
| ----------------------- | ---------------------------------------- |
| `python main.py setup`  | Run initial configuration wizard         |
| `python main.py run`    | Execute a single email processing cycle  |
| `python main.py status` | Display configuration and recent events  |
| `python main.py server` | Start FastAPI server                     |

---

## AI Extraction

For each email, Meridian sends the following data to the LLM:

- Email subject
- Email date
- Plain text email body
- Extracted URLs
- Spreadsheet attachment content (if present)

The model returns structured JSON:

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

Low-confidence extractions are marked with a warning prefix in the generated calendar event.

---

## Idempotent Processing

Each processed email is recorded in SQLite using the Gmail `message_id`.

```
Email arrives
     │
     ▼
Check message_id
     │
  Exists → Skip
     │
  New
     │
     ▼
Extract + Schedule Event
     │
     ▼
Save message_id
```

This guarantees that emails are never processed more than once.

---

## Shortlist Detection

For emails containing shortlist spreadsheets:

1. Meridian downloads `.xlsx` attachments
2. Parses them using `openpyxl`
3. Searches for the user's registration number locally
4. Flags the calendar event if the student appears in the shortlist

The registration number is **never sent to the AI model**.

---

## Security Practices

| Practice                     | Implementation                                                                 |
| ---------------------------- | ------------------------------------------------------------------------------ |
| OAuth 2.0 authentication     | No passwords stored                                                            |
| Minimal API scopes           | Gmail read-only and calendar events only                                       |
| No email storage             | Raw email bodies are never persisted                                           |
| Local token storage          | OAuth tokens remain on the user's machine                                      |
| Registration number privacy  | Never sent to external APIs                                                    |
| Git protection               | Secrets excluded via `.gitignore`                                              |
| CSRF protection              | State parameter validated on OAuth callback to prevent cross-site request forgery |
| XSS prevention               | All user-facing output in the FastAPI server is escaped before rendering       |
| CORS policy                  | Server restricts allowed origins to prevent unauthorized cross-origin requests |
| Prompt injection mitigation  | Email content is passed as data, not instructions; LLM output is validated against a strict JSON schema before use |

Ignored files include:

```
credentials.json
token.json
.env
*.db
```

---

## API Endpoints (Server Mode)

| Method   | Endpoint   | Description                        |
| -------- | ---------- | ---------------------------------- |
| `GET`    | `/health`  | Service health check               |
| `GET`    | `/events`  | List recently scheduled events     |
| `GET`    | `/config`  | Retrieve configuration             |
| `POST`   | `/config`  | Update configuration               |
| `POST`   | `/run`     | Trigger manual pipeline execution  |
| `DELETE` | `/revoke`  | Revoke Google OAuth access         |

Interactive docs available at `http://localhost:8000/docs`.

---

## Troubleshooting

**`credentials.json` not found**
Ensure the OAuth credentials file is placed in the project root.

**No sender IDs configured**
Run `python main.py setup`.

**Deadline already passed**
The email was processed successfully, but the deadline had already expired.

**Low-confidence event warnings**
Verify the deadline in the original email before applying.

---

## Author

Developed as a personal automation tool for managing internship application deadlines from placement emails.
