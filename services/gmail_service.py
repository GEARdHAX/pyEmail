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
from googleapiclient.errors import HttpError
from services.logger import get_logger

logger = get_logger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/spreadsheets']
READ_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
SCOPES_WITH_READ = SCOPES + [READ_SCOPE]

class AuthRequired(RuntimeError):
    """A fresh interactive sign-in is needed but this run is not allowed to open a browser."""


class GmailService:
    def __init__(self, credentials_file, token_file, scopes=None, interactive=True):
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
                if not interactive:  # unattended jobs must fail, never wait for a browser consent
                    raise AuthRequired('Google sign-in expired or missing a scope; run a command interactively once')
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, scopes)
                self.creds = flow.run_local_server(port=0)
            with open(token_file, 'w') as token:
                token.write(self.creds.to_json())
        self.service = build('gmail', 'v1', credentials=self.creds)

    def send_email(self, to, subject, body, attachment_path=None, subtype='html', html_body=None):
        message = MIMEMultipart()
        message['to'] = to
        message['subject'] = Header(subject, 'utf-8') if not subject.isascii() else subject
        if html_body is not None:
            # multipart/alternative: the exact template text AND a tracked HTML copy of it
            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(body, 'plain', 'utf-8'))
            alt.attach(MIMEText(html_body, 'html', 'utf-8'))
            message.attach(alt)
        else:
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

    def find_bounces(self, days=45, max_messages=200):
        """Delivery-failure notices from mailer-daemon/postmaster (metadata + snippet only)."""
        q = f'(from:mailer-daemon OR from:postmaster) newer_than:{days}d'
        out, page = [], None
        while len(out) < max_messages:
            resp = self.service.users().messages().list(
                userId='me', q=q, maxResults=100, pageToken=page).execute()
            for m in resp.get('messages', []):
                full = self.service.users().messages().get(
                    userId='me', id=m['id'], format='metadata',
                    metadataHeaders=['Subject', 'From', 'Content-Type', 'X-Failed-Recipients']).execute()
                hdr = {h.get('name', '').lower(): h.get('value', '')
                       for h in full.get('payload', {}).get('headers', [])}
                out.append({'id': m['id'], 'internalDate': _to_int(full.get('internalDate')),
                            'subject': hdr.get('subject', ''), 'from': hdr.get('from', ''),
                            'content_type': hdr.get('content-type', ''),
                            'failed': hdr.get('x-failed-recipients', ''), 'snippet': full.get('snippet', '')})
            page = resp.get('nextPageToken')
            if not page:
                break
        return out[:max_messages]

    THREAD_HEADERS = ['From', 'Subject', 'Content-Type', 'Auto-Submitted', 'Precedence', 'X-Autoreply',
                      'X-Autorespond', 'X-Auto-Response-Suppress', 'List-Id', 'List-Unsubscribe']

    def get_thread_messages(self, thread_id):
        """Messages of one thread (metadata + snippet only): id, internalDate, labelIds, headers."""
        thread = self.service.users().threads().get(
            userId='me', id=thread_id, format='metadata', metadataHeaders=self.THREAD_HEADERS).execute()
        out = []
        for m in thread.get('messages', []):
            hdr = {h.get('name', '').lower(): h.get('value', '') for h in m.get('payload', {}).get('headers', [])}
            out.append({'id': m.get('id'), 'internalDate': _to_int(m.get('internalDate')),
                        'labelIds': m.get('labelIds', []), 'headers': hdr, 'snippet': m.get('snippet', '')})
        return out

    def get_message_meta(self, message_id):
        """{'threadId', 'internalDate'} of one message (e.g. a legacy send), or None if it is gone."""
        try:
            m = self.service.users().messages().get(userId='me', id=message_id, format='minimal').execute()
        except HttpError as e:
            if getattr(e.resp, 'status', None) == 404:
                return None
            raise
        return {'threadId': m.get('threadId'), 'internalDate': _to_int(m.get('internalDate'))}


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
