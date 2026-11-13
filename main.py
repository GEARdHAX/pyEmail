#!/usr/bin/env python3
import argparse
import os
import json
import sys
from config import GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, RESUME_PATH, DRY_RUN, ENABLE_EMAIL_SENDING
from services.logger import get_logger
from services.tracking_service import TrackingService
from services.email_template_service import EmailTemplateService
from services.duplicate_prevention_service import DuplicatePreventionService

logger = get_logger(__name__)

def validate_configs():
    """Validate required configuration files."""
    errors = []

    if not GOOGLE_SHEET_ID:
        errors.append("GOOGLE_SHEET_ID not set in .env")

    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        errors.append(f"Credentials file not found: {GOOGLE_CREDENTIALS_FILE}")

    if not os.path.exists(GMAIL_TOKEN_FILE):
        errors.append(f"Token file not found: {GMAIL_TOKEN_FILE}")

    if not os.path.exists(RESUME_PATH):
        errors.append(f"Resume file not found: {RESUME_PATH}")

    return errors

def main():
    parser = argparse.ArgumentParser(description="Email Outreach Automation CLI")
    parser.add_argument('mode', choices=['preview', 'send', 'report', 'validate'], help="Execution mode")
    args = parser.parse_args()

    if args.mode == 'validate':
        logger.info("Validating configurations...")
        errors = validate_configs()

        if errors:
            print("Configuration validation FAILED:")
            for error in errors:
                print(f"  - {error}")
            logger.error(f"Validation failed with {len(errors)} errors")
            sys.exit(1)
        else:
            print("✓ Configuration validation PASSED")
            logger.info("All configuration files are present")
            print(f"Sheet ID: {GOOGLE_SHEET_ID}")
            print(f"Dry Run: {DRY_RUN}")
            print(f"Enable Email Sending: {ENABLE_EMAIL_SENDING}")
            print("\nValidation complete!")

    elif args.mode == 'preview':
        if DRY_RUN or not ENABLE_EMAIL_SENDING:
            logger.warning("Safety lock active: DRY_RUN=True or ENABLE_EMAIL_SENDING=False. No emails will be sent.")

        logger.info("Running in PREVIEW mode. No emails will be sent.")
        print("Previewing outreach data...")

    elif args.mode == 'send':
        if DRY_RUN or not ENABLE_EMAIL_SENDING:
            logger.warning("Safety lock active: DRY_RUN=True or ENABLE_EMAIL_SENDING=False. No emails will be sent.")
            print("Sending disabled in config.")
            return

        logger.info("Processing outreach sending...")

    elif args.mode == 'report':
        logger.info("Generating report...")
        print("Outreach Report: 0 sent, 0 pending.")

if __name__ == '__main__':
    main()
