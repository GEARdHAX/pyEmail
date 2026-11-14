"""CLI reports from the tracking store.

Honesty rules baked in:
- Opens/clicks are counted only for messages that were actually sent with tracking; messages
  sent without the pixel/links are reported as "untracked", never as "not opened".
- "Delivered" is shown only as *confirmed by evidence* (a real open, click or reply). No bounce
  seen does NOT mean delivered, so the remainder is reported as "unconfirmed".
- Bounce/reply numbers (and their rates) are shown as n/a until the matching ingest has run.
- Opens are reported both raw (every fetch that passed de-duplication) and "real" (excluding
  fetches that look like scanners or arrive implausibly soon after sending).
"""
from datetime import date, datetime, timezone


def _bound(day, end=False):
    if not day:
        return None
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()


def _group_key(row, by):
    if by == 'campaign':
        return row['campaign'] or '(none)'
    if by == 'tab':
        return row['tab'] or '(none)'
    if by == 'recipient':
        return row['recipient']
    if by == 'day':
        return (row['sent_at'] or 'undated')[:10]
    return 'ALL'


def _rate(num, den):
    return None if not den else num / den


def _summarise(rows, ingested):
    tracked = [r for r in rows if r['tracking_enabled']]
    linked = [r for r in tracked if r['link_count']]
    opened = [r for r in tracked if r['opens_real']]
    clicked = [r for r in linked if r['clicks_real']]
    replied = [r for r in rows if r['replies']]
    perm = [r for r in rows if r['bounces_perm']]
    temp = [r for r in rows if r['bounces_temp'] and not r['bounces_perm']]
    confirmed = [r for r in rows if r['opens_real'] or r['clicks_real'] or r['replies']]
    return {
        'sent': len(rows),
        'untracked': len(rows) - len(tracked),
        'delivered_confirmed': len(confirmed),
        'delivered_unconfirmed': len(rows) - len(confirmed) - len(perm),
        'opens_raw': sum(r['opens_total'] for r in tracked),
        'opens_real': sum(r['opens_real'] for r in tracked),
        'unique_opens': len(opened),
        'clicks_raw': sum(r['clicks_total'] for r in linked),
        'clicks_real': sum(r['clicks_real'] for r in linked),
        'unique_clicks': len(clicked),
        'bounced': len(perm),
        'bounce_temporary': len(temp),
        'replies': len(replied),
        'open_rate': _rate(len(opened), len(tracked)),
        'click_rate': _rate(len(clicked), len(linked)),
        'bounce_rate': _rate(len(perm), len(rows)) if ingested['bounce'] else None,
        'reply_rate': _rate(len(replied), len(rows)) if ingested['reply'] else None,
        'tracked': len(tracked),
    }


def build_report(store, by=None, campaign=None, tab=None, recipient=None, since=None, until=None):
    rows = store.message_rows(state='sent', campaign=campaign, tab=tab, recipient=recipient,
                              since=_bound(since), until=_bound(until))
    ingested = {'bounce': bool(store.meta_get('last_bounce_ingest')),
                'reply': bool(store.meta_get('last_reply_ingest'))}
    groups = {}
    for r in rows:
        groups.setdefault(_group_key(r, by), []).append(r)
    table = [dict(group=k, **_summarise(v, ingested)) for k, v in sorted(groups.items())]
    if by:  # always add a total row for grouped output
        table.append(dict(group='TOTAL', **_summarise(rows, ingested)))
    notes = []
    if not ingested['bounce']:
        notes.append('bounces: not ingested yet (run `track ingest --apply`); bounce rate is n/a')
    if not ingested['reply']:
        notes.append('replies: not ingested yet (run `track ingest --apply`); reply rate is n/a')
    undated = sum(1 for r in rows if not r['sent_at'])
    if undated:
        notes.append(f'{undated} message(s) have no send timestamp (sent before tracking existed)')
    if (since or until) and any(not r['sent_at'] for r in store.message_rows(state='sent')):
        notes.append('date filters exclude messages sent before tracking existed (no timestamp)')
    uncertain = len(store.uncertain_messages())
    if uncertain:
        notes.append(f'{uncertain} message(s) are in an uncertain send state: run `track uncertain`')
    if rows and not any(r['tracking_enabled'] for r in rows):
        notes.append('no message in this selection was sent with tracking: open/click metrics are n/a')
    return table, notes


def _fmt_rate(value, denominator_note=False):
    return 'n/a' if value is None else f"{value * 100:.1f}%"


def format_report(table, notes):
    if not table:
        return 'No sent messages match.\n' + ''.join(f'  note: {n}\n' for n in notes)
    cols = [('group', 'GROUP', 28), ('sent', 'SENT', 5), ('delivered_confirmed', 'DELIV*', 6),
            ('delivered_unconfirmed', 'UNCONF', 6), ('unique_opens', 'U.OPEN', 6), ('opens_real', 'OPENS', 5),
            ('unique_clicks', 'U.CLK', 5), ('clicks_real', 'CLICKS', 6), ('bounced', 'BOUNCE', 6),
            ('bounce_temporary', 'TEMP', 4), ('replies', 'REPLY', 5), ('open_rate', 'OPEN%', 6),
            ('click_rate', 'CLICK%', 6), ('bounce_rate', 'BNC%', 6), ('reply_rate', 'RPL%', 6)]
    lines = [' '.join(h.ljust(w) for _, h, w in cols)]
    for row in table:
        cells = []
        for key, _, w in cols:
            v = row[key]
            if key.endswith('_rate'):
                v = _fmt_rate(v)
            elif key in ('unique_opens', 'opens_real') and row['tracked'] == 0:
                v = 'n/a'
            elif key in ('unique_clicks', 'clicks_real') and row['tracked'] == 0:
                v = 'n/a'
            cells.append(str(v)[:w].ljust(w))
        lines.append(' '.join(cells))
    lines.append('')
    lines.append('* DELIV = delivered, confirmed only by a real open, click or reply. UNCONF = no evidence either way')
    lines.append('  (a missing bounce notice is not proof of delivery). OPENS/CLICKS exclude scanner-like and too-fast')
    lines.append('  fetches and cover tracked messages only. Rates use tracked (open/click) or all sent (bounce/reply).')
    lines += [f'  note: {n}' for n in notes]
    return '\n'.join(lines) + '\n'
