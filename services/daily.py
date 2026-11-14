"""The daily routine in one command:  python run.py daily [--send] [--limit N] [--yes]

    1. Safety check     show the four safety locks
    2. Refresh          ingest new replies/bounces from Gmail, sync Merge status to the Sheet
    3. Dashboard        how many rows are in each status, per tab, plus today's new bounces/replies
    4. Plan             what a send would do right now (nothing is sent in this step)
    5. Attention list   rows a human should fix: missing data, bad emails, unknown statuses, duplicates...
    6. Send (--send)    only if the locks are open AND you type the confirmation phrase; capped by --limit

Everything runs under the shared run lock. Any refresh failure stops the routine before any send.
The log (logs/daily.log) holds counts only: no addresses, no message text.
"""
import re
from collections import Counter, OrderedDict

from services.outreach_service import EMAIL_RE, _norm
from services.refresh import get_refresh_logger, refresh, sanitize
from services.run_lock import RunLock

DEFAULT_LIMIT = 100   # per run; keeps well under Gmail's daily caps and protects sender reputation
ATTENTION = OrderedDict([
    ('SKIP_MISSING_FIELDS', 'row is missing data the template needs (fill the empty cell)'),
    ('SKIP_INVALID_EMAIL', 'email address is not valid'),
    ('SKIP_UNKNOWN_STATUS', 'Merge status has a value pyEmail does not recognise'),
    ('FLAG_BOUNCE_TEMPORARY', 'temporary delivery problem: decide whether to retry by clearing the status'),
    ('TAB_CONFIG_ERROR', 'tab is missing a header or column the template needs'),
    ('TAB_NOT_FOUND', 'configured tab does not exist in the spreadsheet'),
    ('SEND_FAILED', 'Gmail rejected or errored on this send'),
])
SUPPRESSED = ('FLAG_BOUNCED', 'SKIP_RESPONDED', 'SKIP_UNSUBSCRIBED', 'SKIP_DO_NOT_TRACK', 'SKIP_NO_RECIPIENT',
              'SKIP_REPLIED_ELSEWHERE', 'SKIP_BOUNCED_ELSEWHERE')


def _col(index, header):
    return index.get(_norm(header))


def status_table(sheets, sheet_id, cfg):
    """{tab: Counter(status)} for every row that has a valid email. Blank status = 'PENDING'."""
    table = OrderedDict()
    hrow = cfg['header_row']
    for tab, tcfg in cfg['tabs'].items():
        values = sheets.get_tab_values(sheet_id, tab)
        if len(values) < hrow:
            table[tab] = Counter()
            continue
        index = {}
        for i, h in enumerate(values[hrow - 1]):
            index.setdefault(_norm(h), i)
        e, s = _col(index, tcfg['email_header']), _col(index, cfg['merge_status_header'])
        counts = Counter()
        if e is not None and s is not None:
            for row in values[hrow:]:
                email = str(row[e]).strip() if e < len(row) else ''
                if EMAIL_RE.match(email):
                    status = str(row[s]).strip().upper() if s < len(row) else ''
                    counts[status or 'PENDING'] += 1
        table[tab] = counts
    return table


def find_duplicates(sheets, sheet_id, cfg):
    """{tab: [(email, [row numbers])]} for addresses that appear on more than one row of the SAME tab."""
    found = {}
    hrow = cfg['header_row']
    for tab, tcfg in cfg['tabs'].items():
        values = sheets.get_tab_values(sheet_id, tab)
        if len(values) < hrow:
            continue
        index = {}
        for i, h in enumerate(values[hrow - 1]):
            index.setdefault(_norm(h), i)
        e = _col(index, tcfg['email_header'])
        if e is None:
            continue
        seen = {}
        for n, row in enumerate(values[hrow:], start=hrow + 1):
            email = str(row[e]).strip().casefold() if e < len(row) else ''
            if EMAIL_RE.match(email):
                seen.setdefault(email, []).append(n)
        dups = [(k, v) for k, v in seen.items() if len(v) > 1]
        if dups:
            found[tab] = dups
    return found


def _short(tab):
    return tab.split(' - ', 1)[-1] if ' - ' in tab else tab


def run_daily(outreach, cfg, lock_path, log_path, *, send=False, limit=DEFAULT_LIMIT, assume_yes=False,
              locks_open=False, interactive=True, days=45, say=print, ask=input, lock_timeout=120):
    """Returns {'status': ..., ...}. `say`/`ask` are injectable so the routine is fully testable."""
    log = get_refresh_logger(log_path)
    store, gmail, writer = outreach.store, outreach.gmail, outreach.writer
    out = {'status': None}
    lock = RunLock(lock_path)
    if not lock.acquire(lock_timeout):
        say("Another pyEmail job (the scheduled refresh?) is still running. Try again in a minute.")
        log.error("daily status=BLOCKED reason=lock_busy")
        return {'status': 'BUSY'}
    try:
        # 1 ---------------------------------------------------------------- safety
        say("=" * 70)
        say("1/6  SAFETY LOCKS")
        say(f"     sending is {'UNLOCKED (DRY_RUN off, ENABLE_EMAIL_SENDING on)' if locks_open else 'LOCKED (preview only)'}")

        # 2 ---------------------------------------------------------------- refresh
        say("2/6  REFRESH  (new replies/bounces from Gmail -> Sheet)")
        try:
            info = refresh(store, gmail, writer, outreach.track, days)
        except Exception as e:
            say(f"     FAILED: {type(e).__name__}: {sanitize(e)}")
            say("     Stopping here. Nothing was sent. Fix the problem above and run again.")
            log.error(f"daily status=REFRESH_FAILED error={type(e).__name__}")
            return {'status': 'REFRESH_FAILED', 'error': type(e).__name__}
        ev, new = info['events'], info['new_detections']
        say(f"     OK. Gmail shows: {ev.get('reply', 0)} reply, {ev.get('bounce_permanent', 0)} permanent bounce, "
            f"{ev.get('bounce_temporary', 0)} temporary bounce, {ev.get('out_of_office', 0)} out-of-office. "
            f"Sheet cells updated: {info['sheet_writes']}")
        if new:
            say("     NEW since last run:")
            for d in new:
                say(f"       - {d['category'].replace('_', ' '):17} [{_short(d['tab'])}] {d['to']}")
        else:
            say("     Nothing new since the last run.")
        out['refresh'] = info

        # 3 ---------------------------------------------------------------- dashboard
        say("3/6  STATUS DASHBOARD  (rows per Merge status)")
        table = status_table(outreach.sheets, outreach.sheet_id, cfg)
        order = ['PENDING', 'EMAIL_SENT', 'EMAIL_OPENED', 'RESPONDED', 'BOUNCED', 'BOUNCE_TEMPORARY', 'NO_RECIPIENT']
        for tab, counts in table.items():
            ordered = [f"{k.lower().replace('_', ' ')}={counts[k]}" for k in order if counts.get(k)]
            ordered += [f"{k.lower()}={v}" for k, v in counts.items() if k not in order]
            say(f"     {_short(tab):22} " + (', '.join(ordered) or '(no rows)'))
        out['dashboard'] = {t: dict(c) for t, c in table.items()}

        # 4 ---------------------------------------------------------------- plan (never sends)
        say("4/6  PLAN  (what a send would do right now)")
        outreach.send_enabled, outreach.revalidate = False, False
        plan = outreach.process_all()
        by_status = Counter(r.get('status') for r in plan)
        ready_rows = [r for r in plan if r.get('status') == 'PREVIEW']
        unique = {(r['tab'], r['to'].casefold()): r for r in ready_rows}      # one email per person per tab
        ready = len(unique)
        by_tab_ready = Counter(tab for tab, _ in unique)
        dup_rows = len(ready_rows) - ready
        say(f"     ready to send: {ready}" + ("   (" + ', '.join(f"{_short(t)}: {n}" for t, n in by_tab_ready.items()) + ")"
                                             if ready else "")
            + (f"   [{dup_rows} duplicate-address row(s) will not be emailed twice]" if dup_rows else ""))
        suppressed = sum(by_status.get(k, 0) for k in SUPPRESSED)
        say(f"     skipped because already contacted/suppressed: "
            f"{sum(v for k, v in by_status.items() if k and k.startswith('SKIP_ALREADY'))} already sent/opened, "
            f"{suppressed} bounced/replied/suppressed")
        out['plan'] = {'ready': ready, 'by_status': dict(by_status)}

        # 5 ---------------------------------------------------------------- attention
        say("5/6  NEEDS YOUR ATTENTION")
        items = 0
        for status, why in ATTENTION.items():
            rows = [r for r in plan if r.get('status') == status]
            if rows:
                items += len(rows)
                where = ', '.join(f"{_short(r['tab'])} row {r.get('row', '?')}" for r in rows[:8])
                say(f"     {len(rows):3} x {status}: {why}\n          {where}{' ...' if len(rows) > 8 else ''}")
        dups = find_duplicates(outreach.sheets, outreach.sheet_id, cfg)
        for tab, lst in dups.items():
            items += len(lst)
            say(f"     {len(lst):3} x DUPLICATE ADDRESS in {_short(tab)}: "
                + '; '.join(f"rows {', '.join(map(str, rows))}" for _, rows in lst[:6]) + "  (remove the extra rows)")
        uncertain = store.uncertain_messages()
        if uncertain:
            items += len(uncertain)
            say(f"     {len(uncertain):3} x UNCERTAIN SEND (Gmail may or may not have sent it): "
                f"run  python run.py track uncertain  and check your Sent folder")
        if not items:
            say("     Nothing. All clear.")
        out['attention'] = items

        # 6 ---------------------------------------------------------------- send
        say("6/6  SEND")
        if not send and ready == 0:
            say("     No emails are ready. Add new rows to the Sheet (leave Merge status blank), then run again.")
            out['status'] = 'PREVIEW'
        elif not send:
            say(f"     Preview only. To send the {ready} ready email(s):  python run.py daily --send")
            out['status'] = 'PREVIEW'
        elif ready == 0:
            say("     Nothing to send today.")
            out['status'] = 'NOTHING_TO_SEND'
        elif not locks_open:
            say("     Sending is LOCKED in .env, so nothing was sent. For a one-time unlock without editing .env:")
            say("       PowerShell:  $env:DRY_RUN='false'; $env:ENABLE_EMAIL_SENDING='true'; python run.py daily --send")
            out['status'] = 'LOCKED'
        else:
            to_send = min(ready, limit)
            if not assume_yes:
                if not interactive:
                    say("     Not an interactive terminal and --yes was not given: nothing sent.")
                    return {**out, 'status': 'NEEDS_CONFIRMATION'}
                phrase = f"SEND {to_send}"
                say(f"     About to send {to_send} email(s)" + (f" (cap {limit}; {ready - to_send} will wait for the next run)"
                                                              if ready > to_send else "") + ".")
                answer = ask(f"     Type  {phrase}  to send now, anything else cancels: ")
                if answer.strip() != phrase:
                    say("     Cancelled. Nothing was sent.")
                    log.info("daily status=CANCELLED")
                    return {**out, 'status': 'CANCELLED'}
            outreach.send_enabled, outreach.revalidate, outreach.send_limit = True, True, limit
            results = outreach.process_all()   # lock held + refresh done moments ago + per-recipient final check
            counts = Counter(r.get('status') for r in results)
            sent, failed = counts.get('SENT', 0), counts.get('SEND_FAILED', 0)
            held_back = counts.get('SKIP_DAILY_LIMIT', 0)
            changed = sum(v for k, v in counts.items() if k and k.startswith(('SKIP_ROW', 'SKIP_STATUS', 'SKIP_REVALID')))
            say(f"     DONE. sent={sent}  failed={failed}  held back by cap={held_back}  skipped at final check={changed}")
            if failed:
                say("     Some sends failed. Run  python run.py track uncertain  and check your Sent folder.")
            say("     Bounces arrive minutes later: the scheduled refresh (every 15 min) or tomorrow's  daily  picks them up.")
            out.update(status='SENT', sent=sent, failed=failed, held_back=held_back)
        log.info(f"daily status={out['status']} new_events={len(new)} ready={ready} attention={items} "
                 f"sent={out.get('sent', 0)} failed={out.get('failed', 0)}")
        say("=" * 70)
        return out
    finally:
        lock.release()
