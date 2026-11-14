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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(BASE_DIR, 'data', 'pyemail.lock')   # one Gmail/Sheet/DB job at a time
REFRESH_LOG = os.path.join(BASE_DIR, 'logs', 'refresh.log')
DAILY_LOG = os.path.join(BASE_DIR, 'logs', 'daily.log')

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

def build_store(settings=None):
    from services.tracking_settings import load_settings
    from services.tracking_store import TrackingStore
    settings = settings or load_settings()
    store = TrackingStore(settings.db_path, min_delay=settings.open_min_delay_seconds)
    store.init()
    store.import_legacy('logs/tracking.json')  # idempotent: keeps pre-existing sends covered
    return store, settings

def build_outreach(send, need_read=False):
    from services.gmail_service import GmailService
    from services.google_sheets_service import GoogleSheetsService
    from services.outreach_service import OutreachService, load_mapping
    from services.status_sync import SheetStatusWriter
    from services.gmail_service import SCOPES_WITH_READ
    store, settings = build_store()
    # real sends also read Gmail (refresh-before-send), so they use the wider scope set
    gmail = GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, scopes=SCOPES_WITH_READ if (send or need_read) else None)
    sheets = GoogleSheetsService(gmail.creds)
    cfg = load_mapping()
    writer = SheetStatusWriter(sheets, cfg, GOOGLE_SHEET_ID)
    return sheets, OutreachService(
        sheets, gmail, EmailTemplateService(), DuplicatePreventionService(), TrackingService(),
        cfg, GOOGLE_SHEET_ID, RESUME_PATH, send_enabled=send, store=store, writer=writer, track=settings,
        revalidate=send)

def handle_track(args, only=None):
    """`track` sub-commands: init | serve | ingest | sync | report | uncertain | resolve."""
    from services.reports import build_report, format_report
    sub = args.sub
    if sub not in ('init', 'serve', 'ingest', 'sync', 'report', 'uncertain', 'resolve', 'test-email'):
        print("usage: run.py track {init|serve|ingest|sync|report|uncertain|resolve|test-email} [options]")
        sys.exit(2)
    store, settings = build_store()
    if sub == 'init':
        rows = store.message_rows()
        print(f"tracking DB ready at {settings.db_path}: {len(rows)} messages "
              f"({sum(1 for r in rows if r['legacy'])} imported from logs/tracking.json)")
        print(f"open/click tracking in outgoing mail: {'ENABLED' if settings.enabled else 'disabled (default)'}")
    elif sub == 'serve':
        from services.tracking_server import serve
        serve(store, settings)
    elif sub == 'report':
        table, notes = build_report(store, by=args.by, campaign=args.campaign, tab=args.tab,
                                    recipient=args.recipient, since=args.since, until=args.until)
        print(format_report(table, notes))
    elif sub == 'uncertain':
        rows = store.uncertain_messages()
        for r in rows:
            print(f"{r['state']:9} key={r['send_key']}  tab={r['tab']} row={r['row_no']}  created={r['created_at']}")
        print(f"{len(rows)} unresolved. If you checked Gmail Sent: `track resolve --key KEY --resolve-as sent|unsent`")
    elif sub == 'resolve':
        if not args.key or args.resolve_as not in ('sent', 'unsent'):
            print('usage: run.py track resolve --key "TAB|email" --resolve-as sent|unsent')
            sys.exit(2)
        print('resolved' if store.resolve(args.key, args.resolve_as) else 'nothing to resolve for that key')
    elif sub == 'test-email':
        # Sends one tracked test message to --to (preview unless --apply). Not tied to the spreadsheet.
        from services.gmail_service import GmailService
        from services.test_email import send_test_email
        if not args.to:
            print('usage: run.py track test-email --to ADDRESS [--apply]')
            sys.exit(2)
        if args.apply and (DRY_RUN or not ENABLE_EMAIL_SENDING):
            # same safety lock as `send`: --apply alone is not enough to put a real email on the wire
            logger.warning("Safety lock active: DRY_RUN=True or ENABLE_EMAIL_SENDING=False. No email will be sent.")
            print("Sending disabled in config (DRY_RUN / ENABLE_EMAIL_SENDING). Nothing sent.")
            sys.exit(1)
        gmail = GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE) if args.apply else None
        try:
            r = send_test_email(store, gmail, settings, args.to, apply=args.apply)
        except ValueError as e:
            print(f"cannot run: {e}")
            sys.exit(1)
        print(f"{r['status']}: to={r['to']} subject={r['subject']!r}" + (f" ({r.get('detail')})" if r.get('detail') else ""))
        if r['status'] == 'PREVIEW':
            print(r['body'])
            print("[preview only - add --apply to send this one email]")
    elif sub == 'ingest':
        # Reads Gmail only. Preview by default; --apply records events in the LOCAL tracking DB.
        # Neither mode writes to Google Sheets: that is `track sync --apply`.
        from services.gmail_service import GmailService, SCOPES_WITH_READ
        from services.google_sheets_service import GoogleSheetsService
        from services.event_ingest import CATEGORIES, ingest_all
        from services.outreach_service import load_mapping
        from services.status_sync import SheetStatusWriter, sync_statuses
        gmail = GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, scopes=SCOPES_WITH_READ)
        summary = ingest_all(store, gmail, args.days, apply=args.apply)
        for d in summary['detections']:
            if d['category'] in ('reply', 'bounce_permanent', 'bounce_temporary'):
                state = 'recorded' if d['applied_now'] else ('already recorded' if d['recorded'] else 'NEW')
                print(f"[{d['tab']}] row {d['row']}: {d['category']:17} {d['to']}  at {d['at']}  ({state})")
        print()
        print("Events detected by category (this run):" + ("" if args.apply else "  [PREVIEW - nothing recorded]"))
        for cat in CATEGORIES:
            n, new = summary['counts'].get(cat, 0), summary['new'].get(cat, 0)
            extra = f"  ({new} not yet recorded)" if cat in ('reply', 'bounce_permanent', 'bounce_temporary') and n else ""
            print(f"  {cat:20} {n}{extra}")
        un = summary['unresolved']
        print(f"Unresolved: {un.get('no_thread', 0)} message(s) without a findable Gmail thread, "
              f"{un.get('thread_error', 0)} thread fetch error(s)")
        if args.apply:
            print(f"Backfilled Gmail thread/send time for {summary['backfilled']} older message(s)")
        sheets = GoogleSheetsService(gmail.creds)
        writer = SheetStatusWriter(sheets, load_mapping(), GOOGLE_SHEET_ID)
        proposed = [r for r in sync_statuses(store, writer, settings, apply=False, overlay=summary['overlay'])
                    if r['result'] not in ('ALREADY_SET',)]
        print()
        print("Proposed Merge status changes (NOTHING written to the sheet):")
        for r in proposed:
            tag = '  [from detected-but-unrecorded events]' if r.get('from_pending_evidence') else ''
            print(f"  [{r['tab']}] row {r['row']}: {r['current'] or 'blank'} -> {r['desired']}  {r['result']}  {r['to']}{tag}")
        if not proposed:
            print("  none")
        print()
        print("" + ("Events recorded locally. Review above, then run `track sync` (preview) and `track sync --apply`."
                      if args.apply else "[preview only - add --apply to record events locally; the sheet is only changed by `track sync --apply`]"))
    elif sub == 'sync':
        from services.gmail_service import GmailService
        from services.google_sheets_service import GoogleSheetsService
        from services.outreach_service import load_mapping
        from services.status_sync import SheetStatusWriter, sync_statuses
        sheets = GoogleSheetsService(GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE).creds)
        writer = SheetStatusWriter(sheets, load_mapping(), GOOGLE_SHEET_ID)
        results = sync_statuses(store, writer, settings, apply=args.apply)
        shown = [r for r in results if r['result'] != 'ALREADY_SET']
        for r in shown:
            print(f"[{r['tab']}] row {r['row']}: {r['result']} {r['to']} ({r['current'] or 'blank'} -> {r['desired']})")
        print(f"{len(shown)} change(s) needing attention, {len(results) - len(shown)} already in sync" + ("" if args.apply else "  [preview only - add --apply to write]"))

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
    parser.add_argument('mode', choices=['preview', 'send', 'report', 'validate', 'tabs', 'fix-headers', 'replies', 'bounces', 'track', 'colors', 'refresh', 'daily'], help="Execution mode")
    parser.add_argument('sub', nargs='?', help='track: init|serve|ingest|sync|report|uncertain|resolve')
    parser.add_argument('--full', type=int, default=0, metavar='N',
                        help="preview: print complete rendered emails for the first N previewable rows per tab")
    parser.add_argument('--only', metavar='"TAB NAME:ROW"',
                        help='process just one sheet row, e.g. "01 - HR and Recruiter:58"')
    parser.add_argument('--apply', action='store_true',
                        help='replies: write RESPONDED to Merge status (default is preview only)')
    parser.add_argument('--days', type=int, default=45, help='replies: look back this many days (default 45)')
    parser.add_argument('--by', choices=['campaign', 'tab', 'day', 'recipient'], help='track report: group by')
    parser.add_argument('--campaign', help='track report: filter by campaign')
    parser.add_argument('--tab', help='track report: filter by tab name')
    parser.add_argument('--recipient', help='track report: filter by recipient address')
    parser.add_argument('--since', metavar='YYYY-MM-DD', help='track report: sent on/after (UTC)')
    parser.add_argument('--until', metavar='YYYY-MM-DD', help='track report: sent before (UTC)')
    parser.add_argument('--send', action='store_true', help='daily: after the report, send the ready emails (asks you to confirm)')
    parser.add_argument('--limit', type=int, default=None, metavar='N',
                        help='send/daily: stop after N send attempts this run (daily default 100)')
    parser.add_argument('--yes', action='store_true', help='daily --send: skip the typed confirmation')
    parser.add_argument('--to', help='track test-email: recipient (your own address)')
    parser.add_argument('--key', help='track resolve: send key "TAB|email"')
    parser.add_argument('--resolve-as', dest='resolve_as', choices=['sent', 'unsent'], help='track resolve outcome')
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

    elif args.mode == 'colors':
        # Colour-codes the Merge status column (formatting only; no cell values change). Preview unless --apply.
        from services.gmail_service import GmailService
        from services.google_sheets_service import GoogleSheetsService
        from services.outreach_service import load_mapping
        from services.sheet_colors import apply_status_colors
        sheets = GoogleSheetsService(GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE).creds)
        report = apply_status_colors(sheets.service, GOOGLE_SHEET_ID, sheets, load_mapping(), apply=args.apply)
        for r in report:
            print(f"[{r['tab']}] {r.get('status') or '-':17} {r.get('color', ''):7} {r['result']}")
        print("" if args.apply else "[preview only - add --apply to add the colour rules]")

    elif args.mode == 'track':
        handle_track(args)

    elif args.mode == 'bounces':
        # Reads Gmail bounce notices (needs gmail.readonly); preview unless --apply.
        from services.gmail_service import GmailService, SCOPES_WITH_READ
        from services.google_sheets_service import GoogleSheetsService
        from services.outreach_service import load_mapping
        from services.bounce_service import BounceService
        gmail = GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, scopes=SCOPES_WITH_READ)
        svc = BounceService(GoogleSheetsService(gmail.creds), gmail, load_mapping(), GOOGLE_SHEET_ID, args.days)
        results = svc.check_all(apply=args.apply)
        for r in results:
            print(f"[{r['tab']}] row {r['row']}: {r['result']} {r['to']} (was {r['status']})")
        print()
        print(f"{len({r['to'] for r in results})} bounced addresses across {len(results)} rows"
              + ("" if args.apply else "  [preview only - add --apply to write BOUNCED]"))

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
        from services.refresh import send_campaign
        from services.run_lock import RunBusy
        _, outreach = build_outreach(send=True)
        outreach.only = only
        outreach.send_limit = args.limit
        try:
            # takes the run lock, refreshes replies/bounces + Sheet, re-checks each recipient, then sends
            results = send_campaign(outreach, LOCK_PATH, REFRESH_LOG, days=args.days)
        except RunBusy as e:
            print(f"NOT SENDING: {e}")
            sys.exit(1)
        except Exception as e:
            from services.refresh import sanitize
            print(f"NOT SENDING: the pre-send refresh could not complete ({type(e).__name__}: {sanitize(e)}). "
                  f"Nothing was sent.")
            logger.error(f"send aborted before any email: {type(e).__name__}")
            sys.exit(1)
        print_results(results)

    elif args.mode == 'daily':
        # The whole daily routine: refresh -> dashboard -> plan -> attention list -> (optional) send.
        from services.daily import DEFAULT_LIMIT, run_daily
        locks_open = (not DRY_RUN) and ENABLE_EMAIL_SENDING
        _, outreach = build_outreach(send=False, need_read=True)
        result = run_daily(outreach, outreach.cfg, LOCK_PATH, DAILY_LOG, send=args.send,
                           limit=args.limit if args.limit else DEFAULT_LIMIT, assume_yes=args.yes,
                           locks_open=locks_open, interactive=sys.stdin.isatty(), days=args.days)
        sys.exit(1 if result['status'] in ('REFRESH_FAILED', 'BUSY') else 0)

    elif args.mode == 'refresh':
        # Unattended job (Task Scheduler): ingest replies/bounces + sync the Sheet. NEVER sends email.
        from services.gmail_service import GmailService, SCOPES_WITH_READ
        from services.google_sheets_service import GoogleSheetsService
        from services.outreach_service import load_mapping
        from services.refresh import run_refresh_job
        from services.status_sync import SheetStatusWriter

        def build():
            gmail = GmailService(GOOGLE_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, scopes=SCOPES_WITH_READ,
                                 interactive=False)   # an expired sign-in must fail, never open a browser
            store, settings = build_store()
            writer = SheetStatusWriter(GoogleSheetsService(gmail.creds), load_mapping(), GOOGLE_SHEET_ID)
            return store, gmail, writer, settings

        code, info = run_refresh_job(build, LOCK_PATH, REFRESH_LOG, days=args.days)
        print(f"refresh: {info['status']}" + (f" ({info['error']})" if info.get('error') else ""))
        sys.exit(code)

    elif args.mode == 'report':
        logger.info("Generating report...")
        print("Outreach Report: 0 sent, 0 pending.")

if __name__ == '__main__':
    main()
