"""Persistent tracking store (SQLite): messages, tracked links and an append-only event log.

Design notes
- One `messages` row per (tab, recipient) -- the same key the duplicate-prevention
  service uses -- reserved BEFORE Gmail is called so that a crash between "Gmail accepted"
  and "we recorded it" can never lead to a second email (state stays `sending`/`uncertain`
  until a human resolves it).
- Tracking ids / link ids are random (secrets), never derived from the address or row.
- Events are append-only; `dedupe_key` makes reply/bounce ingestion idempotent and open/click
  events are de-duplicated inside a short window per client.
- Only a salted hash of the client address and a truncated user-agent are stored.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from services.logger import get_logger

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  send_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('sending','sent','uncertain')),
  tracking_id TEXT NOT NULL UNIQUE,
  campaign TEXT, tab TEXT, row_no INTEGER, recipient TEXT NOT NULL, subject TEXT,
  gmail_message_id TEXT, gmail_thread_id TEXT,
  tracking_enabled INTEGER NOT NULL DEFAULT 0,
  legacy INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, sent_at TEXT,
  sheet_status TEXT, sheet_synced_at TEXT, sync_note TEXT
);
CREATE INDEX IF NOT EXISTS messages_recipient ON messages(recipient);
CREATE TABLE IF NOT EXISTS links(
  link_id TEXT PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  UNIQUE(message_id, url)
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  type TEXT NOT NULL CHECK(type IN ('open','click','reply','bounce','bounce_temp')),
  occurred_at TEXT NOT NULL,
  link_id TEXT, source_ref TEXT, actor_hash TEXT, ua TEXT,
  suspect INTEGER NOT NULL DEFAULT 0,
  detail TEXT,
  dedupe_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS events_msg_type ON events(message_id, type);
"""

DUPLICATE_WINDOW_SECONDS = 10
SCANNER_UA = re.compile(
    r'(barracuda|proofpoint|mimecast|safelinks|bitdefender|zscaler|urldefense|trendmicro|forcepoint|'
    r'symantec|python-requests|python-urllib|curl/|wget/|headless|phantomjs|bot\b|crawler|spider|scanner)', re.I)
CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(text):
    return datetime.fromisoformat(text) if text else None


def classify_fetch(ua, seconds_since_sent, min_delay):
    """Reasons a pixel/link fetch is probably NOT a person reading the mail ([] = looks human)."""
    reasons = []
    if ua and SCANNER_UA.search(ua):
        reasons.append('scanner_user_agent')
    if seconds_since_sent is not None and seconds_since_sent < min_delay:
        reasons.append('too_soon_after_send')
    return reasons


class TrackingStore:
    def __init__(self, db_path='data/tracking.db', clock=utcnow, min_delay=20):
        self.db_path, self.clock, self.min_delay = db_path, clock, min_delay

    # -- plumbing ---------------------------------------------------------
    def _connect(self):
        if self.db_path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    @contextmanager
    def _tx(self):
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.execute('COMMIT')
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()

    def init(self):
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('salt',?)", (secrets.token_hex(16),))
        finally:
            conn.close()

    def meta_get(self, key, default=None):
        with self._tx() as c:
            r = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return r['value'] if r else default

    def meta_set(self, key, value):
        with self._tx() as c:
            c.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (key, str(value)))

    def actor_hash(self, client_id):
        salt = self.meta_get('salt') or ''
        return hashlib.sha256((salt + str(client_id)).encode()).hexdigest()[:16]

    # -- sending lifecycle ------------------------------------------------
    def reserve_send(self, send_key, tab, row_no, recipient, campaign, subject, tracking_enabled):
        """Atomically claim a send. Returns (state, row): 'reserved' | existing state."""
        with self._tx() as c:
            row = c.execute('SELECT * FROM messages WHERE send_key=?', (send_key,)).fetchone()
            if row:
                return row['state'], dict(row)
            tid = secrets.token_urlsafe(16)
            c.execute(
                'INSERT INTO messages(send_key,state,tracking_id,campaign,tab,row_no,recipient,subject,'
                'tracking_enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (send_key, 'sending', tid, campaign, tab, row_no, recipient.lower(), subject,
                 1 if tracking_enabled else 0, iso(self.clock())))
            return 'reserved', dict(c.execute('SELECT * FROM messages WHERE send_key=?', (send_key,)).fetchone())

    def finalize_sent(self, message_id, gmail_message_id, thread_id=None):
        with self._tx() as c:
            c.execute("UPDATE messages SET state='sent', gmail_message_id=?, gmail_thread_id=?, sent_at=? WHERE id=?",
                      (gmail_message_id, thread_id, iso(self.clock()), message_id))

    def mark_uncertain(self, message_id):
        with self._tx() as c:
            c.execute("UPDATE messages SET state='uncertain' WHERE id=? AND state!='sent'", (message_id,))

    def release(self, message_id):
        """Drop a reservation after a failure that provably did not send anything."""
        with self._tx() as c:
            c.execute("DELETE FROM messages WHERE id=? AND state!='sent'", (message_id,))

    def resolve(self, send_key, outcome):
        """Human decision for a `sending`/`uncertain` row: 'sent' keeps it blocked, 'unsent' frees it."""
        with self._tx() as c:
            row = c.execute('SELECT * FROM messages WHERE send_key=?', (send_key,)).fetchone()
            if not row or row['state'] == 'sent':
                return False
            if outcome == 'unsent':
                c.execute('DELETE FROM messages WHERE id=?', (row['id'],))
            elif outcome == 'sent':
                c.execute("UPDATE messages SET state='sent' WHERE id=?", (row['id'],))
            else:
                raise ValueError("outcome must be 'sent' or 'unsent'")
            return True

    def import_legacy(self, tracking_json_path):
        """Bring pre-existing duplicate-prevention records in so reports and sync cover them."""
        if not os.path.exists(tracking_json_path):
            return 0
        with open(tracking_json_path, encoding='utf-8') as f:
            legacy = json.load(f)
        added = 0
        with self._tx() as c:
            for key, gmail_id in legacy.items():
                tab, _, recipient = key.partition('|')
                if c.execute('SELECT 1 FROM messages WHERE send_key=?', (key,)).fetchone():
                    continue
                c.execute(
                    "INSERT INTO messages(send_key,state,tracking_id,campaign,tab,recipient,gmail_message_id,"
                    "tracking_enabled,legacy,created_at) VALUES(?,?,?,?,?,?,?,0,1,?)",
                    (key, 'sent', secrets.token_urlsafe(16), tab, tab, recipient.lower(), gmail_id,
                     iso(self.clock())))
                added += 1
        return added

    # -- links -------------------------------------------------------------
    def add_link(self, message_id, url):
        with self._tx() as c:
            r = c.execute('SELECT link_id FROM links WHERE message_id=? AND url=?', (message_id, url)).fetchone()
            if r:
                return r['link_id']
            link_id = secrets.token_urlsafe(9)
            c.execute('INSERT INTO links(link_id,message_id,url) VALUES(?,?,?)', (link_id, message_id, url))
            return link_id

    def get_link(self, link_id):
        with self._tx() as c:
            r = c.execute('SELECT * FROM links WHERE link_id=?', (link_id,)).fetchone()
            return dict(r) if r else None

    def get_message(self, message_id):
        with self._tx() as c:
            r = c.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
            return dict(r) if r else None

    def get_message_by_key(self, send_key):
        with self._tx() as c:
            r = c.execute('SELECT * FROM messages WHERE send_key=?', (send_key,)).fetchone()
            return dict(r) if r else None

    # -- events ------------------------------------------------------------
    def _fetch_event(self, kind, message_id, link_id, client_id, ua):
        now = self.clock()
        actor = self.actor_hash(client_id)
        ua = CONTROL.sub('', ua or '')[:200]
        with self._tx() as c:
            m = c.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
            if not m:
                return 'unknown'
            since = iso(now - timedelta(seconds=DUPLICATE_WINDOW_SECONDS))
            dup = c.execute(
                "SELECT 1 FROM events WHERE message_id=? AND type=? AND actor_hash=? AND COALESCE(link_id,'')=? "
                "AND occurred_at>=?", (message_id, kind, actor, link_id or '', since)).fetchone()
            if dup:
                return 'duplicate'
            sent_at = parse_iso(m['sent_at'])
            age = (now - sent_at).total_seconds() if sent_at else None
            reasons = classify_fetch(ua, age, self.min_delay)
            if 'googleimageproxy' in ua.lower():
                reasons_detail = reasons + ['gmail_image_proxy']  # real open, but IP/caching are masked
            else:
                reasons_detail = reasons
            c.execute(
                'INSERT INTO events(message_id,type,occurred_at,link_id,actor_hash,ua,suspect,detail) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (message_id, kind, iso(now), link_id, actor, ua, 1 if reasons else 0, json.dumps(reasons_detail)))
            return 'recorded_suspect' if reasons else 'recorded'

    def record_open(self, tracking_id, client_id, ua):
        with self._tx() as c:
            m = c.execute('SELECT id FROM messages WHERE tracking_id=?', (tracking_id,)).fetchone()
        if not m:
            return 'unknown'
        return self._fetch_event('open', m['id'], None, client_id, ua)

    def record_click(self, link_id, client_id, ua):
        link = self.get_link(link_id)
        if not link:
            return 'unknown'
        return self._fetch_event('click', link['message_id'], link_id, client_id, ua)

    def add_event(self, message_id, kind, occurred_at, source_ref, detail, dedupe_key):
        """Idempotent insert for reply/bounce events. Returns True when a new row was written."""
        with self._tx() as c:
            cur = c.execute(
                'INSERT OR IGNORE INTO events(message_id,type,occurred_at,source_ref,detail,dedupe_key) '
                'VALUES(?,?,?,?,?,?)',
                (message_id, kind, iso(occurred_at) if isinstance(occurred_at, datetime) else occurred_at,
                 source_ref, json.dumps(detail) if not isinstance(detail, str) else detail, dedupe_key))
            return cur.rowcount == 1

    def has_event(self, dedupe_key):
        with self._tx() as c:
            return c.execute('SELECT 1 FROM events WHERE dedupe_key=?', (dedupe_key,)).fetchone() is not None

    def update_message_meta(self, message_id, thread_id=None, sent_at=None):
        """Backfill Gmail ids/timestamps recovered later. Fills EMPTY fields only; never overwrites."""
        with self._tx() as c:
            if thread_id:
                c.execute('UPDATE messages SET gmail_thread_id=? WHERE id=? AND gmail_thread_id IS NULL',
                          (thread_id, message_id))
            if sent_at:
                c.execute('UPDATE messages SET sent_at=? WHERE id=? AND sent_at IS NULL', (sent_at, message_id))

    # -- queries -----------------------------------------------------------
    _AGG = """
      SELECT m.*,
        (SELECT COUNT(*) FROM events e WHERE e.message_id=m.id AND e.type='open') AS opens_total,
        (SELECT COUNT(*) FROM events e WHERE e.message_id=m.id AND e.type='open' AND e.suspect=0) AS opens_real,
        (SELECT COUNT(*) FROM events e WHERE e.message_id=m.id AND e.type='click') AS clicks_total,
        (SELECT COUNT(*) FROM events e WHERE e.message_id=m.id AND e.type='click' AND e.suspect=0) AS clicks_real,
        (SELECT COUNT(*) FROM events e WHERE e.message_id=m.id AND e.type='reply') AS replies,
        (SELECT COUNT(*) FROM events e WHERE e.message_id=m.id AND e.type='bounce') AS bounces_perm,
        (SELECT COUNT(*) FROM events e WHERE e.message_id=m.id AND e.type='bounce_temp') AS bounces_temp,
        (SELECT MIN(occurred_at) FROM events e WHERE e.message_id=m.id AND e.type='open' AND e.suspect=0) AS first_open,
        (SELECT MAX(occurred_at) FROM events e WHERE e.message_id=m.id AND e.type='open' AND e.suspect=0) AS last_open,
        (SELECT COUNT(*) FROM links l WHERE l.message_id=m.id) AS link_count
      FROM messages m
    """

    def message_rows(self, *, state=None, campaign=None, tab=None, recipient=None, since=None, until=None):
        where, args = [], []
        if state:
            where.append('m.state=?'); args.append(state)
        if campaign:
            where.append('m.campaign=?'); args.append(campaign)
        if tab:
            where.append('m.tab=?'); args.append(tab)
        if recipient:
            where.append('m.recipient=?'); args.append(recipient.lower())
        if since:
            where.append('m.sent_at>=?'); args.append(since)
        if until:
            where.append('m.sent_at<?'); args.append(until)
        sql = self._AGG + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY m.id'
        with self._tx() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def blocked_reason(self, recipient):
        """'REPLIED' / 'BOUNCED' if ANY tab's message to this address has such evidence, else None."""
        with self._tx() as c:
            r = c.execute(
                "SELECT e.type FROM events e JOIN messages m ON m.id=e.message_id "
                "WHERE m.recipient=? AND e.type IN ('reply','bounce') "
                "ORDER BY CASE e.type WHEN 'reply' THEN 0 ELSE 1 END LIMIT 1", (recipient.lower(),)).fetchone()
        if not r:
            return None
        return 'REPLIED' if r['type'] == 'reply' else 'BOUNCED'

    def set_sheet_status(self, message_id, status, note=None):
        with self._tx() as c:
            c.execute('UPDATE messages SET sheet_status=?, sheet_synced_at=?, sync_note=? WHERE id=?',
                      (status, iso(self.clock()), note, message_id))

    def uncertain_messages(self):
        with self._tx() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM messages WHERE state IN ('sending','uncertain') ORDER BY id").fetchall()]
