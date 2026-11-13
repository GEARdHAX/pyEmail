import os
from dotenv import load_dotenv

load_dotenv()

# Basic config
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
RESUME_PATH = os.getenv("RESUME_PATH", "assets/resume.pdf")

# Mode flags
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ENABLE_EMAIL_SENDING = os.getenv("ENABLE_EMAIL_SENDING", "false").lower() == "true"

# Services
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
RESUME_PATH = os.getenv("RESUME_PATH", "assets/resume.pdf")

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ENABLE_EMAIL_SENDING = os.getenv("ENABLE_EMAIL_SENDING", "false").lower() == "true"
