import glob
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


def get_credentials(token_file, scopes, client_secret_glob="credentials/backend/client_secret*.json"):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_secret_file = glob.glob(client_secret_glob)[0]
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return creds
