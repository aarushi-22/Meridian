from auth.google_auth import get_credentials

def main():
    creds = get_credentials()
    print("Authentication successful!")

if __name__ == "__main__":
    main()
