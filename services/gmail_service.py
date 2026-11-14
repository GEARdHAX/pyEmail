from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import os
import json
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.header import Header
from googleapiclient.discovery import build
from services.logger import get_logger

logger = get_logger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/spreadsheets']
READ_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
SCOPES_WITH_READ = SCOPES + [READ_SCOPE]

class GmailService:
    def __init__(self, credentials_file, token_file, scopes=None):
        scopes = list(scopes or SCOPES)
        self.creds = None
        if os.path.exists(token_file):
            self.creds = Credentials.from_authorized_user_file(token_file, scopes)
            try:
                with open(token_file) as f:
                    granted = set(json.load(f).get('scopes') or [])
            except (OSError, ValueError):
                granted = set()
            if not set(scopes) <= granted:
                self.creds = None  # a newly required scope: ask for consent again
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, scopes)
                self.creds = flow.run_local_server(port=0)
            with open(token_file, 'w') as token:
                token.write(self.creds.to_json())
        self.service = build('gmail', 'v1', credentials=self.creds)

    def send_email(self, to, subject, body, attachment_path=None, subtype='html'):
        message = MIMEMultipart()
        message['to'] = to
        message['subject'] = Header(subject, 'utf-8') if not subject.isascii() else subject
        message.attach(MIMEText(body, subtype, 'utf-8'))

        if attachment_path:
            if not os.path.exists(attachment_path):
                raise FileNotFoundError(f"Attachment not found: {attachment_path}")
            with open(attachment_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(attachment_path))
                part['Content-Disposition'] = f'attachment; filename="{os.path.basename(attachment_path)}"'
                message.attach(part)

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        return self.service.users().messages().send(userId='me', body={'raw': raw_message}).execute()

    # -- read-only helpers (need the gmail.readonly scope) -------------
    def first_sent_ms(self, address, days=45):
        """internalDate (ms) of the earliest message we sent to `address` in the window, or None."""
        resp = self.service.users().messages().list(
            userId='me', q=f'to:{address} in:sent newer_than:{days}d', maxResults=10).execute()
        stamps = []
        for m in resp.get('messages', []):
            full = self.service.users().messages().get(userId='me', id=m['id'], format='minimal').execute()
            stamps.append(int(full['internalDate']))
        return min(stamps) if stamps else None

    def find_replies(self, address, days=45):
        """Inbound messages from `address` in the window (metadata only, no bodies)."""
        resp = self.service.users().messages().list(
            userId='me', q=f'from:{address} -in:sent newer_than:{days}d', maxResults=10).execute()
        out = []
        for m in resp.get('messages', []):
            full = self.service.users().messages().get(
                userId='me', id=m['id'], format='metadata',
                metadataHeaders=['From', 'Subject', 'Auto-Submitted', 'Precedence', 'X-Autoreply']).execute()
            hdr = {h['name'].lower(): h['value'] for h in full.get('payload', {}).get('headers', [])}
            out.append({'id': m['id'], 'threadId': full.get('threadId'),
                        'internalDate': int(full['internalDate']), 'headers': hdr})
        return out
