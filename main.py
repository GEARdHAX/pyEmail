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

def build_outreach(send):
    from services.gmail_service import GmailService
    from services.google_sheets_service import GoogleSheetsService
    from services.outreach_service import OutreachService, load_mapping
    gmail = GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE)
    sheets = GoogleSheetsService(gmail.creds)
    return sheets, OutreachService(
        sheets, gmail, EmailTemplateService(), DuplicatePreventionService(), TrackingService(),
        load_mapping(), GOOGLE_SHEET_ID, RESUME_PATH, send_enabled=send)

def print_results(results, full=0):
    shown = {}
    for r in results:
        print(f"[{r['tab']}] row {r.get('row', '-')}: {r['status']} {r.get('to', '')} {r.get('missing', '') or r.get('detail', '')}")
        if r['status'] == 'PREVIEW':
            print(f"    Subject: {r['subject']}")
            if shown.get(r['tab'], 0) < full:
                shown[r['tab']] = shown.get(r['tab'], 0) + 1
                print(f"    To: {r['to']}   Attachment: {r['attach'] or 'none'}")
                print("    Placeholders: " + json.dumps(r['placeholders'], ensure_ascii=False))
                print("    ---- body ----")
                print(r['body'])
                print("    --------------")

def main():
    parser = argparse.ArgumentParser(description="Email Outreach Automation CLI")
    parser.add_argument('mode', choices=['preview', 'send', 'report', 'validate', 'tabs', 'fix-headers', 'replies'], help="Execution mode")
    parser.add_argument('--full', type=int, default=0, metavar='N',
                        help="preview: print complete rendered emails for the first N previewable rows per tab")
    parser.add_argument('--only', metavar='"TAB NAME:ROW"',
                        help='process just one sheet row, e.g. "01 - HR and Recruiter:58"')
    parser.add_argument('--apply', action='store_true',
                        help='replies: write RESPONDED to Merge status (default is preview only)')
    parser.add_argument('--days', type=int, default=45, help='replies: look back this many days (default 45)')
    args = parser.parse_args()
    only = None
    if args.only:
        tab_name, _, row_no = args.only.rpartition(':')
        only = (tab_name, int(row_no))

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
        _, outreach = build_outreach(send=False)
        outreach.only = only
        print_results(outreach.process_all(), args.full)

    elif args.mode == 'fix-headers':
        # Opt-in: adds missing header names (no data cells) to the follow-up tab only.
        _, outreach = build_outreach(send=False)
        tab = next(t for t, c in outreach.cfg['tabs'].items() if c['template'] == 'followup')
        added = outreach.ensure_headers(tab, [outreach.cfg['merge_status_header'], 'Your_Original_Subject_Line'])
        print(f"{tab!r}: added headers {added}" if added else f"{tab!r}: nothing to add")

    elif args.mode == 'replies':
        # Reads Gmail (needs the gmail.readonly scope: first run asks for consent again).
        from services.gmail_service import GmailService, SCOPES_WITH_READ
        from services.google_sheets_service import GoogleSheetsService
        from services.outreach_service import load_mapping
        from services.reply_service import ReplyService
        gmail = GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, scopes=SCOPES_WITH_READ)
        svc = ReplyService(GoogleSheetsService(gmail.creds), gmail, load_mapping(), GOOGLE_SHEET_ID, args.days)
        results = svc.check_all(apply=args.apply)
        for r in results:
            print(f"[{r['tab']}] row {r['row']}: {r['result']} {r['to']} (was {r['status']})")
        print(f"\nchecked {len(results)} rows; replied: {sum(r['result'].startswith('REPLIED') for r in results)}"
              + ("" if args.apply else "  [preview only - add --apply to write RESPONDED]"))

    elif args.mode == 'tabs':
        from services.gmail_service import GmailService
        from services.google_sheets_service import GoogleSheetsService
        sheets = GoogleSheetsService(GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE).creds)
        for tab in sheets.list_tabs(GOOGLE_SHEET_ID):
            rows = sheets.get_tab_values(GOOGLE_SHEET_ID, tab)
            print(f"TAB {tab!r}: headers (row 1) = {rows[0] if rows else None}")

    elif args.mode == 'send':
        if DRY_RUN or not ENABLE_EMAIL_SENDING:
            logger.warning("Safety lock active: DRY_RUN=True or ENABLE_EMAIL_SENDING=False. No emails will be sent.")
            print("Sending disabled in config.")
            return

        logger.info("Processing outreach sending...")
        _, outreach = build_outreach(send=True)
        outreach.only = only
        print_results(outreach.process_all())

    elif args.mode == 'report':
        logger.info("Generating report...")
        print("Outreach Report: 0 sent, 0 pending.")

if __name__ == '__main__':
    main()
