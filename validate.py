#!/usr/bin/env python3
import sys
import json
from config import GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, RESUME_PATH
from services.logger import get_logger
from services.tracking_service import TrackingService
from services.email_template_service import EmailTemplateService
from services.duplicate_prevention_service import DuplicatePreventionService

logger = get_logger(__name__)

if not GOOGLE_SHEET_ID:
    logger.error("GOOGLE_SHEET_ID not set in config")
    sys.exit(1)

if not GOOGLE_CREDENTIALS_FILE:
    logger.error("GOOGLE_CREDENTIALS_FILE not set in config")
    sys.exit(1)

if not GMAIL_TOKEN_FILE:
    logger.error("GMAIL_TOKEN_FILE not set in config")
    sys.exit(1)

print("Config loaded successfully")
print(f"Sheet ID: {GOOGLE_SHEET_ID}")
print(f"Credentials File: {GOOGLE_CREDENTIALS_FILE}")
print(f"Token File: {GMAIL_TOKEN_FILE}")
print(f"Resume Path: {RESUME_PATH}")
print(f"Dry Run: {config.DRY_RUN}")
print(f"Enable Email Sending: {config.ENABLE_EMAIL_SENDING}")

# Testing core services
logger.info("Testing services...")

try:
    template_service = EmailTemplateService()
    print("✓ EmailTemplateService initialized")
except Exception as e:
    print(f"✗ EmailTemplateService failed: {e}")
    sys.exit(1)

try:
    duplicate_service = DuplicatePreventionService()
    print("✓ DuplicatePreventionService initialized")
except Exception as e:
    print(f"✗ DuplicatePreventionService failed: {e}")
    sys.exit(1)

try:
    tracking_service = TrackingService()
    print("✓ TrackingService initialized")
except Exception as e:
    print(f"✗ TrackingService failed: {e}")
    sys.exit(1)

print("\nValidation complete!")
print(f"Sheet ID: {GOOGLE_SHEET_ID}")
print(f"Dry Run: {config.DRY_RUN}")
print(f"Enable Email Sending: {config.ENABLE_EMAIL_SENDING}")
