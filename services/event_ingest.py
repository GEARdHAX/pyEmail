"""Turn Gmail evidence (thread replies, bounce notices) into events in the tracking store.

Gmail is only ever READ (gmail.readonly). The Sheet is not touched here; `status_sync` does that
afterwards, under the status policy, when you run `track sync --apply`.

Two complementary scans, both idempotent (a dedupe key is built from the Gmail message id):

1. Thread scan  - for every sent message we look at its own Gmail thread. Legacy sends that were
   recorded without a thread id get it from their Gmail message id (and a send timestamp).
   Each inbound message is classified (see message_classifier):
       human            -> `reply` event (first one per message)
       bounce_permanent -> `bounce` event          bounce_temporary -> `bounce_temp` event
       out_of_office / automated / bounce_unclassified -> counted, never stored, never a reply
2. Notice scan  - delivery-failure notices from mailer-daemon/postmaster found by search, matched to
   the most recent preceding send to the failed address. Malformed notices are counted, not guessed.

`apply=False` is a pure preview: nothing is written, not even backfilled timestamps.
"""
import re
from collections import Counter
from datetime import datetime, timezone

from services.bounce_service import ADDR_RE, PERMANENT, TEMPORARY, classify_notice, notice_kind
from services.logger import get_logger
from services.message_classifier import (AUTOMATED, BOUNCE_PERMANENT, BOUNCE_TEMPORARY, BOUNCE_UNCLASSIFIED, HUMAN,
                                         OUT_OF_OFFICE, classify_message)
from services.tracking_store import iso, parse_iso

logger = get_logger(__name__)

FROM_ADDR = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

# detection category -> stored event type (categories not listed here are counted, never stored)
PERSIST = {'reply': 'reply', BOUNCE_PERMANENT: 'bounce', BOUNCE_TEMPORARY: 'bounce_temp'}
CATEGORIES = ('reply', OUT_OF_OFFICE, AUTOMATED, BOUNCE_PERMANENT, BOUNCE_TEMPORARY, BOUNCE_UNCLASSIFIED,
              'bounce_unattributed', 'malformed')
ALL = set(CATEGORIES)


def _valid_ms(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _ms_to_dt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(microsecond=0)


def _ms(iso_text):
    return int(parse_iso(iso_text).timestamp() * 1000) if iso_text else None


def _best_message(rows, noticed_at, sent_times):
    """The message a bounce most plausibly belongs to: the latest one sent BEFORE the notice.

    `sent_times` maps message id -> aware datetime for sends whose time is known (from our own
    record or looked up from Gmail). A message with no known send time is never a candidate:
    attributing a notice without being able to check the order would be guessing.
    """
    dated = [(sent_times[r['id']], r) for r in rows if sent_times.get(r['id']) and sent_times[r['id']] <= noticed_at]
    return max(dated, key=lambda t: t[0])[1] if dated else None


class Run:
    """Accumulates detections for one ingest run, de-duplicated by key across both scans."""

    def __init__(self, store, apply, want):
        self.store, self.apply, self.want = store, apply, set(want)
        self.detections, self.seen = [], set()
        self.unresolved = Counter()
        self.backfilled = 0
        self.sent_times = {}  # message id -> datetime of the send, as far as it is known

    def add(self, category, key, message=None, at=None, source_id=None, detail=None, **extra):
        if category not in self.want or key in self.seen:
            return None
        self.seen.add(key)
        d = {'category': category, 'key': key, 'source_id': source_id, 'at': at, 'detail': detail or {},
             'tab': message['tab'] if message else None, 'row': message['row_no'] if message else None,
             'to': message['recipient'] if message else None, 'message_id': message['id'] if message else None,
             'recorded': None, 'new': None, 'applied_now': False, **extra}
        etype = PERSIST.get(category)
        if etype and message:
            if self.apply:
                d['new'] = self.store.add_event(message['id'], etype, at, source_id, d['detail'], key)
                d['recorded'] = True
                d['applied_now'] = bool(d['new'])
            else:
                d['new'] = not self.store.has_event(key)
                d['recorded'] = not d['new']
        self.detections.append(d)
        return d


def _thread_info(run, gmail, m):
    """(thread_id, sent_ms) for a recorded message, recovering legacy values from Gmail when needed."""
    thread, sent_ms = m['gmail_thread_id'], _ms(m['sent_at'])
    if (not thread or sent_ms is None) and m['gmail_message_id'] and hasattr(gmail, 'get_message_meta'):
        try:
            meta = gmail.get_message_meta(m['gmail_message_id'])
        except Exception as e:
            logger.warning(f"could not look up Gmail message for a recorded send: {type(e).__name__}")
            run.unresolved['thread_error'] += 1   # an API failure (a missing message returns None instead)
            meta = None
        if meta:
            new_thread = meta.get('threadId')
            new_ms = _valid_ms(meta.get('internalDate'))
            if run.apply and ((not thread and new_thread) or (m['sent_at'] is None and new_ms)):
                run.store.update_message_meta(m['id'], thread_id=new_thread if not thread else None,
                                              sent_at=iso(_ms_to_dt(new_ms)) if (new_ms and m['sent_at'] is None) else None)
                run.backfilled += 1
            thread = thread or new_thread
            sent_ms = sent_ms if sent_ms is not None else new_ms
    if sent_ms is not None:
        run.sent_times[m['id']] = _ms_to_dt(sent_ms)
    return thread, sent_ms


def scan_threads(run, gmail):
    for m in run.store.message_rows(state='sent'):
        thread, sent_ms = _thread_info(run, gmail, m)
        if not thread:
            run.unresolved['no_thread'] += 1
            continue
        try:
            messages = gmail.get_thread_messages(thread)
        except Exception as e:
            logger.warning(f"thread fetch failed ({type(e).__name__}); will retry next run")
            run.unresolved['thread_error'] += 1
            continue
        replied = False
        for x in messages or []:
            if not isinstance(x, dict) or not x.get('id'):
                run.add('malformed', f"malformed:thread:{thread}:{len(run.seen)}", m, detail={'where': 'thread'})
                continue
            at_ms = _valid_ms(x.get('internalDate'))
            labels = x.get('labelIds') or []
            if 'SENT' in labels or 'DRAFT' in labels or x['id'] == m['gmail_message_id']:
                continue  # our own messages
            if at_ms is None:
                run.add('malformed', f"malformed:{x['id']}", m, source_id=x['id'], detail={'where': 'no_internalDate'})
                continue
            if sent_ms is not None and at_ms <= sent_ms:
                continue  # older than our email: not a reply to it
            headers, snippet = x.get('headers') or {}, x.get('snippet') or ''
            cat = classify_message(headers, snippet)
            at = _ms_to_dt(at_ms)
            if cat == HUMAN:
                if replied:
                    continue
                replied = True
                sender = (FROM_ADDR.search(str(headers.get('from', ''))) or [None])[0]
                run.add('reply', f"reply:{x['id']}", m, at=at, source_id=x['id'],
                        detail={'from': (sender or '').lower(), 'thread': thread}, sender=sender, thread=thread)
            elif cat in (BOUNCE_PERMANENT, BOUNCE_TEMPORARY):
                etype = PERSIST[cat]
                run.add(cat, f"{etype}:{x['id']}:{m['recipient']}", m, at=at, source_id=x['id'],
                        detail={'notice': x['id']})
            else:  # out_of_office / automated / bounce_unclassified: evidence of nothing; just counted
                run.add(cat, f"{cat}:{x['id']}", m, at=at, source_id=x['id'])


def scan_notices(run, gmail, days):
    rows = run.store.message_rows(state='sent')
    by_recipient = {}
    for r in rows:
        by_recipient.setdefault(r['recipient'], []).append(r)
        if r['id'] not in run.sent_times and r['sent_at']:
            run.sent_times[r['id']] = parse_iso(r['sent_at'])
    known = set(by_recipient)
    for notice in gmail.find_bounces(days) or []:
        if not isinstance(notice, dict) or not notice.get('id'):
            run.add('malformed', f"malformed:notice:{len(run.seen)}", detail={'where': 'notice'})
            continue
        nid = notice['id']
        kind = notice_kind(notice)
        if kind is None:
            continue  # mail from a daemon address that is not a delivery notice
        ms = _valid_ms(notice.get('internalDate'))
        if ms is None:
            run.add('malformed', f"malformed:{nid}", source_id=nid, detail={'where': 'no_internalDate'})
            continue
        if kind == 'unclassified':
            run.add(BOUNCE_UNCLASSIFIED, f"{BOUNCE_UNCLASSIFIED}:{nid}", source_id=nid, at=_ms_to_dt(ms))
            continue
        text = ' '.join(v for v in (notice.get('failed'), notice.get('snippet')) if isinstance(v, str))
        if not ADDR_RE.search(text):
            run.add('malformed', f"malformed:{nid}", source_id=nid, detail={'where': 'bounce_without_recipient'})
            continue
        attributed = classify_notice(notice, known)
        if not attributed:
            run.add('bounce_unattributed', f"bounce_unattributed:{nid}", source_id=nid, at=_ms_to_dt(ms),
                    detail={'kind': kind})
            continue
        noticed_at = _ms_to_dt(ms)
        for addr, k in attributed.items():
            msg = _best_message(by_recipient[addr], noticed_at, run.sent_times)
            if not msg:
                run.add('bounce_unattributed', f"bounce_unattributed:{nid}:{addr}", source_id=nid, at=noticed_at,
                        detail={'reason': 'no earlier send known'})
                continue
            cat = BOUNCE_PERMANENT if k == PERMANENT else BOUNCE_TEMPORARY
            etype = PERSIST[cat]
            run.add(cat, f"{etype}:{nid}:{addr}", msg, at=noticed_at, source_id=nid, detail={'notice': nid})


def _summary(run):
    counts, new = Counter(), Counter()
    overlay = {}
    for d in run.detections:
        counts[d['category']] += 1
        if d['new']:
            new[d['category']] += 1
            if not run.apply:  # preview: pending evidence the status sync should take into account
                o = overlay.setdefault(d['message_id'], {'replies': 0, 'bounces_perm': 0, 'bounces_temp': 0})
                o[{'reply': 'replies', BOUNCE_PERMANENT: 'bounces_perm', BOUNCE_TEMPORARY: 'bounces_temp'}[d['category']]] += 1
    return {'detections': run.detections, 'counts': counts, 'new': new, 'overlay': overlay,
            'unresolved': dict(run.unresolved), 'backfilled': run.backfilled, 'applied': run.apply}


def ingest_all(store, gmail, days=45, apply=False):
    """Full ingest (replies, bounces, auto-mail). Preview unless apply=True."""
    run = Run(store, apply, ALL)
    scan_threads(run, gmail)      # exact: tied to each sent message's own thread
    scan_notices(run, gmail, days)  # address-level: notices that are not threaded with the original
    if apply:
        now = iso(store.clock())
        store.meta_set('last_bounce_ingest', now)
        store.meta_set('last_reply_ingest', now)
    return _summary(run)


# --- narrower entry points kept for compatibility -------------------------------------------------
def ingest_bounces(store, gmail, days=45, apply=False):
    run = Run(store, apply, {BOUNCE_PERMANENT, BOUNCE_TEMPORARY})
    scan_notices(run, gmail, days)
    if apply:
        store.meta_set('last_bounce_ingest', iso(store.clock()))
    return [{'tab': d['tab'], 'row': d['row'], 'to': d['to'],
             'kind': PERMANENT if d['category'] == BOUNCE_PERMANENT else TEMPORARY, 'new': d['new']}
            for d in run.detections]


def ingest_replies(store, gmail, apply=False):
    run = Run(store, apply, {'reply'})
    scan_threads(run, gmail)
    if apply:
        store.meta_set('last_reply_ingest', iso(store.clock()))
    replies = [{'tab': d['tab'], 'row': d['row'], 'to': d['to'], 'from': d.get('sender'), 'thread': d.get('thread'),
                'at': iso(d['at']), 'new': d['new']} for d in run.detections]
    return replies, run.unresolved['no_thread']
