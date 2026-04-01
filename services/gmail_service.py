'''
import base64
import os.path
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
def decode_base64(data):
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
def readID(creds):
    try:
    # Call the Gmail API
        service = build("gmail", "v1", credentials=creds)
        results = service.users().messages().list(userId="me", q="chennai.pat@vit.ac.in", maxResults = 5).execute()
        messages = results.get("messages", [])

        #if not labels:
         #   print("No labels found.")
         #   return
        #print("Messages:")
        #for message in messages:
        #    print(message["id"])

    except HttpError as error:
    # TODO(developer) - Handle errors from gmail API.
        print(f"An error occurred: {error}")
    return messages

def readMessage(creds,message_id):
    service = build("gmail", "v1", credentials=creds)
    data = service.users().messages().get( userId="me", id=message_id, format="metadata").execute()
    headers = data["payload"]["headers"]
    subject = sender = receriver = None

    for h in headers:
        if h["name"] == "Subject":
            subject = h["value"]
        elif h["name"] == "From":
            sender = h["value"]
        elif h["name"] == "To":
            receiver = h["value"]
    print("Message id:",message_id)
    print("From:",sender)
    #print("To:",receiver)
    print("Subject:",subject)
    print()

def getMail(creds,message_id):
    service = build("gmail", "v1", credentials=creds)
    message = service.users().messages().get( userId="me", id=message_id, format="full").execute()
    payload = message["payload"]

    if payload.get("body", {}).get("data"):
        text = decode_base64(payload["body"]["data"])
        print(text)
        return
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                body = part["body"].get("data")
                if body:
                    text = base64.urlsafe_b64decode(body).decode("utf-8", errors="ignore")
                    print(text)
                    return

    print("No readable body found.")
    return None
'''
"""
services/gmail_service.py
Gmail API wrapper for Meridian.

Builds on the starter code — adds:
  - Full body extraction (plain text + HTML fallback)
  - Excel / CSV attachment parsing for round-2/3 shortlists
  - Sender filtering against user config
  - History-based incremental fetch (only emails since last poll)
  - Returns structured dicts instead of printing
"""

import base64
import re
import io
from typing import Optional
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ── Helpers ───────────────────────────────────────────────────────────────────

def _b64_decode(data: str) -> str:
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")


def _strip_html(html: str) -> str:
    """Very lightweight HTML -> plain text. No deps needed."""
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _extract_urls(text: str) -> list[str]:
    """Pull all http/https URLs out of a string."""
    return re.findall(r'https?://[^\s\)\]>\"\']+', text)


def _parse_attachment(part: dict, service, user_id: str = "me",
                      message_id: str = "") -> Optional[str]:
    """
    Download and parse an attachment part.
    Returns plain-text table content for .xlsx / .csv, else None.
    """
    filename = part.get("filename", "")
    mime = part.get("mimeType", "")
    attachment_id = part.get("body", {}).get("attachmentId")

    if not attachment_id:
        return None

    # Only bother with spreadsheets
    is_xlsx = filename.endswith((".xlsx", ".xls")) or "spreadsheet" in mime
    is_csv  = filename.endswith(".csv") or mime == "text/csv"
    if not (is_xlsx or is_csv):
        return None

    try:
        att = service.users().messages().attachments().get(
            userId=user_id, messageId=message_id, id=attachment_id
        ).execute()
        raw = base64.urlsafe_b64decode(att["data"])
    except HttpError:
        return None

    try:
        if is_csv:
            return raw.decode("utf-8", errors="ignore")

        # xlsx — use openpyxl
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        lines = []
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=True):
                if any(cell is not None for cell in row):
                    lines.append("\t".join(str(c) if c is not None else "" for c in row))
        return "\n".join(lines)
    except Exception:
        return None


def _walk_parts(payload: dict, service, message_id: str) -> tuple[str, list[str], list[str]]:
    """
    Recursively walk MIME parts.
    Returns (body_text, urls_in_body, attachment_texts)
    """
    body_text = ""
    urls: list[str] = []
    attachments: list[str] = []

    def walk(part):
        nonlocal body_text
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")

        if mime == "text/plain" and data and not body_text:
            body_text = _b64_decode(data)

        elif mime == "text/html" and data and not body_text:
            body_text = _strip_html(_b64_decode(data))

        elif mime.startswith("multipart/"):
            for sub in part.get("parts", []):
                walk(sub)

        # Try attachment
        att_text = _parse_attachment(part, service, message_id=message_id)
        if att_text:
            attachments.append(att_text)

    walk(payload)

    # Ensure we have something
    if not body_text:
        raw_data = payload.get("body", {}).get("data")
        if raw_data:
            body_text = _b64_decode(raw_data)

    urls = _extract_urls(body_text)
    return body_text, urls, attachments


# ── Public API ────────────────────────────────────────────────────────────────

def build_service(creds):
    return build("gmail", "v1", credentials=creds)


def fetch_recent_messages(creds, sender_ids: list[str],
                          max_results: int = 10) -> list[dict]:
    """
    Fetch recent messages from any of the given sender IDs.
    Returns list of {id, threadId}.
    """
    if not sender_ids:
        return []

    service = build_service(creds)
    query = " OR ".join(f"from:{s}" for s in sender_ids)

    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        return result.get("messages", [])
    except HttpError as e:
        print(f"[gmail] Error fetching messages: {e}")
        return []


def fetch_messages_since(creds, sender_ids: list[str],
                         history_id: str) -> list[dict]:
    """
    Efficient incremental fetch using Gmail history API.
    Returns list of new message stubs since history_id.
    Falls back to fetch_recent_messages on error.
    """
    service = build_service(creds)
    try:
        history = service.users().history().list(
            userId="me",
            startHistoryId=history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX"
        ).execute()

        messages = []
        for record in history.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added["message"]
                messages.append({"id": msg["id"], "threadId": msg.get("threadId", "")})
        return messages

    except HttpError:
        return fetch_recent_messages(creds, sender_ids)


def get_message_detail(creds, message_id: str) -> Optional[dict]:
    """
    Fetch and parse a single email into a structured dict.
    Returns:
      {
        message_id, subject, sender, received_at,
        body, urls, attachments,
        raw_headers
      }
    """
    service = build_service(creds)
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except HttpError as e:
        print(f"[gmail] Error fetching message {message_id}: {e}")
        return None

    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    subject  = headers.get("Subject", "(no subject)")
    sender   = headers.get("From", "")
    date_str = headers.get("Date", "")

    body, urls, attachments = _walk_parts(msg["payload"], service, message_id)

    return {
        "message_id": message_id,
        "subject": subject,
        "sender": sender,
        "received_at": date_str,
        "body": body,
        "urls": urls,
        "attachments": attachments,   # list of plain-text tables from xlsx/csv
        "raw_headers": headers,
    }


def get_latest_history_id(creds) -> Optional[str]:
    """Get the current historyId for the inbox (used to seed incremental sync)."""
    service = build_service(creds)
    try:
        profile = service.users().getProfile(userId="me").execute()
        return profile.get("historyId")
    except HttpError:
        return None