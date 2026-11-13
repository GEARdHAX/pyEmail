import os
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from services.logger import get_logger

load_dotenv()

logger = get_logger(__name__)

# Credentials and Config
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
RESUME_PATH = os.getenv("RESUME_PATH", "assets/resume.pdf")

# Mode flags
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ENABLE_EMAIL_SENDING = os.getenv("ENABLE_EMAIL_SENDING", "false").lower() == "true"

# Initialize Services
try:
    gmail_service = None
    sheets_service = None

    if not DRY_RUN and ENABLE_EMAIL_SENDING:
        gmail_service = __import__('services.gmail_service', fromlist=['GmailService']).GmailService(
            GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE
        )

    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE)
    sheets_service = __import__('services.google_sheets_service', fromlist=['GoogleSheetsService']).GoogleSheetsService(
        creds
    )
except Exception as e:
    logger.error(f"Initialization failed: {e}")
    raise
