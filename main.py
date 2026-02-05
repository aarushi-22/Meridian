from auth.google_auth import get_credentials
from services.gmail_service import readID
from services.gmail_service import readMessage
def main():
    creds = get_credentials()
    print("Authentication successful!")
    messages = readID(creds)
    for msg in messages:
        data = readMessage(creds,msg["id"])
        
        

if __name__ == "__main__":
    main()
