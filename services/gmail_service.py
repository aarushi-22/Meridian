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