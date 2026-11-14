"""Mocked integration tests: Sheet writer, status sync, Gmail ingestion, outreach with the tracking store.

Fake Gmail and fake Sheets only. No real emails, no network.
"""
import base64
import email
import os
import re
import tempfile
import unittest
from datetime import timedelta
from email import policy

import httplib2
from googleapiclient.errors import HttpError

from services import status_policy as sp
from services.bounce_service import BounceService, classify_notice
from services.duplicate_prevention_service import DuplicatePreventionService
from services.email_template_service import EmailTemplateService
from services.event_ingest import ingest_bounces, ingest_replies
from services.gmail_service import GmailService
from services.google_sheets_service import GoogleSheetsService
from services.outreach_service import OutreachService
from services.status_sync import SheetStatusWriter, sync_statuses
from services.tracking_service import TrackingService
from services.tracking_settings import TrackingSettings
from tests.test_outreach import MAPPING, RESUME, TPL, sheet_data
from tests.test_tracking_core import T0, Clock, make_store, send


def http_error(status):
    return HttpError(httplib2.Response({'status': status}), b'{}')


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


class FakeSheetsRW(GoogleSheetsService):
    """Reads and writes against an in-memory workbook; supports failure injection."""

    def __init__(self, data):
        self.data = data
        self.updates, self.batches, self.fresh_reads = [], [], []
        self.write_errors = []        # exceptions raised by successive writes
        self.fresh_override = {}      # (tab, row) -> row data returned by the "live" row read
        self.fail_updates = False

    def list_tabs(self, sheet_id):
        return list(self.data)

    def get_tab_values(self, sheet_id, tab):
        return [list(r) for r in self.data[tab]]

    def get_sheet_data(self, sheet_id, range_name):
        m = re.match(r"^'(.*)'!(\d+):(\d+)$", range_name)
        if m:
            tab, row = m.group(1).replace("''", "'"), int(m.group(2))
            self.fresh_reads.append((tab, row))
            if (tab, row) in self.fresh_override:
                return [list(self.fresh_override[(tab, row)])]
            rows = self.data[tab]
            return [list(rows[row - 1])] if row <= len(rows) else []
        m = re.match(r"^'(.*)'!([A-Z]+)(\d+)$", range_name)
        if m:
            tab, c, r = m.group(1).replace("''", "'"), col_index(m.group(2)), int(m.group(3))
            row = self.data[tab][r - 1]
            return [[row[c]]] if c < len(row) else []
        return self.get_tab_values(sheet_id, range_name.strip("'"))

    def _put(self, tab, c, r, value):
        row = self.data[tab][r - 1]
        while len(row) <= c:
            row.append('')
        row[c] = value

    def _maybe_fail(self):
        if self.fail_updates:
            raise RuntimeError('sheets down')
        if self.write_errors:
            raise self.write_errors.pop(0)

    def update_status(self, sheet_id, range_name, status):
        self._maybe_fail()
        m = re.match(r"^'(.*)'!([A-Z]+)(\d+)$", range_name)
        self._put(m.group(1).replace("''", "'"), col_index(m.group(2)), int(m.group(3)), status)
        self.updates.append((range_name, status))

    def update_cells(self, sheet_id, cells):
        self._maybe_fail()
        for t, c, r, v in cells:
            self._put(t, c, r, v)
        self.batches.append([(t, self.column_letter(c) + str(r), v) for t, c, r, v in cells])


CFG = {
    'header_row': 1, 'merge_status_header': 'Merge status', 'sent_value': 'EMAIL_SENT',
    'tabs': {'T': {'template': 'cold', 'email_header': 'Email', 'attach_resume': False, 'field_map': {}}},
}
HEADERS = ['Email', 'Name', 'Merge status', 'Campaign status', 'First opened', 'Open count']


def tab_data(*rows):
    return {'T': [list(HEADERS)] + [list(r) for r in rows]}


def writer_for(sheets, cfg=None, **kw):
    sleeps = []
    w = SheetStatusWriter(sheets, cfg or CFG, 'SID', sleep=sleeps.append, **kw)
    w.sleeps = sleeps
    return w


class WriterTests(unittest.TestCase):
    def test_writes_only_the_status_cell(self):
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'A', 'EMAIL_SENT', 'keep', '', ''],
                                       ['b@x.com', 'B', 'EMAIL_SENT', 'keep', '', '']))
        w = writer_for(sheets)
        r = w.apply('T', 2, 'a@x.com', sp.OPENED)
        self.assertEqual(r['result'], 'WRITTEN')
        self.assertEqual(sheets.updates, [("'T'!C2", 'EMAIL_OPENED')])
        self.assertEqual(sheets.data['T'][1][:2] + sheets.data['T'][1][3:], ['a@x.com', 'A', 'keep', '', ''])
        self.assertEqual(sheets.data['T'][2][2], 'EMAIL_SENT')

    def test_relocates_when_rows_moved_and_unique(self):
        sheets = FakeSheetsRW(tab_data(['z@x.com', 'Z', '', '', '', ''], ['a@x.com', 'A', '', '', '', '']))
        r = writer_for(sheets).apply('T', 2, 'a@x.com', sp.SENT)  # row 2 now holds somebody else
        self.assertEqual((r['result'], r['row']), ('WRITTEN', 3))
        self.assertEqual(sheets.updates, [("'T'!C3", 'EMAIL_SENT')])

    def test_not_found_and_ambiguous_write_nothing(self):
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'A', '', '', '', ''], ['a@x.com', 'A2', '', '', '', '']))
        w = writer_for(sheets)
        self.assertEqual(w.apply('T', 9, 'a@x.com', sp.SENT)['result'], 'ROW_AMBIGUOUS')
        self.assertEqual(w.apply('T', 2, 'nobody@x.com', sp.SENT)['result'], 'ROW_NOT_FOUND')
        # an exact row hint still wins over duplicates when it matches
        self.assertEqual(w.apply('T', 3, 'a@x.com', sp.SENT)['result'], 'WRITTEN')
        self.assertEqual(sheets.updates, [("'T'!C3", 'EMAIL_SENT')])

    def test_precedence_matrix(self):
        cases = [('', sp.OPENED, 'WRITTEN'), (sp.SENT, sp.OPENED, 'WRITTEN'),
                 (sp.OPENED, sp.SENT, 'SKIPPED_STATUS'), (sp.RESPONDED, sp.OPENED, 'SKIPPED_STATUS'),
                 (sp.BOUNCED, sp.RESPONDED, 'CONFLICT'), (sp.OPENED, sp.BOUNCED, 'CONFLICT'),
                 (sp.OPENED, sp.OPENED, 'ALREADY_SET'), ('WEIRD', sp.OPENED, 'SKIPPED_STATUS'),
                 (sp.NO_RECIPIENT, sp.BOUNCED, 'SKIPPED_STATUS'), (sp.BOUNCE_TEMPORARY, sp.OPENED, 'WRITTEN'),
                 (sp.OPENED, sp.RESPONDED, 'WRITTEN')]
        for current, new, expected in cases:
            sheets = FakeSheetsRW(tab_data(['a@x.com', 'A', current, '', '', '']))
            r = writer_for(sheets).apply('T', 2, 'a@x.com', new)
            self.assertEqual(r['result'], expected, (current, new))
            self.assertEqual(sheets.data['T'][1][2], new if expected == 'WRITTEN' else current)

    def test_read_before_write_catches_concurrent_changes(self):
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'A', 'EMAIL_SENT', '', '', '']))
        w = writer_for(sheets)
        w.begin_run()
        w._tab('T')  # snapshot taken while the status was EMAIL_SENT
        sheets.fresh_override[('T', 2)] = ['a@x.com', 'A', 'RESPONDED', '', '', '']  # changed since
        self.assertEqual(w.apply('T', 2, 'a@x.com', sp.OPENED)['result'], 'STATUS_CHANGED')
        sheets.fresh_override[('T', 2)] = ['someone@else.com', 'A', 'EMAIL_SENT', '', '', '']  # row replaced
        self.assertEqual(w.apply('T', 2, 'a@x.com', sp.OPENED)['result'], 'ROW_CHANGED')
        self.assertEqual(sheets.updates, [])

    def test_preview_writes_nothing(self):
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'A', 'EMAIL_SENT', '', '', '']))
        self.assertEqual(writer_for(sheets).apply('T', 2, 'a@x.com', sp.OPENED, apply=False)['result'], 'WOULD_WRITE')
        self.assertEqual((sheets.updates, sheets.batches, sheets.fresh_reads), ([], [], []))

    def test_cannot_write_no_recipient_or_garbage(self):
        w = writer_for(FakeSheetsRW(tab_data(['a@x.com', 'A', '', '', '', ''])))
        for bad in (sp.NO_RECIPIENT, 'WHATEVER', ''):
            with self.assertRaises(ValueError):
                w.apply('T', 2, 'a@x.com', bad)

    def test_transient_errors_are_retried_with_backoff(self):
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'A', 'EMAIL_SENT', '', '', '']))
        sheets.write_errors = [http_error(503), http_error(429)]
        w = writer_for(sheets)
        self.assertEqual(w.apply('T', 2, 'a@x.com', sp.OPENED)['result'], 'WRITTEN')
        self.assertEqual(w.sleeps, [1, 2])

    def test_permanent_and_exhausted_errors_do_not_crash_or_loop(self):
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'A', 'EMAIL_SENT', '', '', '']))
        sheets.write_errors = [http_error(403)]
        w = writer_for(sheets)
        self.assertEqual(w.apply('T', 2, 'a@x.com', sp.OPENED)['result'], 'WRITE_FAILED')
        self.assertEqual(w.sleeps, [])
        sheets.write_errors = [http_error(503)] * 10
        self.assertEqual(w.apply('T', 2, 'a@x.com', sp.OPENED)['result'], 'WRITE_FAILED')
        self.assertEqual(len(w.sleeps), 3)  # 4 attempts -> 3 waits
        self.assertEqual(sheets.data['T'][1][2], 'EMAIL_SENT')

    def test_tracking_columns_are_opt_in_and_batched(self):
        cfg = {**CFG, 'tracking_columns': {'first_open_at': 'First opened', 'open_count': 'Open count'}}
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'A', 'EMAIL_SENT', 'keep', '', '']))
        w = writer_for(sheets, cfg)
        r = w.apply('T', 2, 'a@x.com', sp.OPENED, tracking_values={'first_open_at': 'T1', 'open_count': 2,
                                                                     'click_count': 9})
        self.assertEqual(r['result'], 'WRITTEN')
        self.assertEqual(sheets.updates, [])
        self.assertEqual(len(sheets.batches), 1)
        self.assertEqual(sorted(sheets.batches[0]), sorted([('T', 'C2', 'EMAIL_OPENED'), ('T', 'E2', 'T1'),
                                                            ('T', 'F2', 2)]))  # click_count: no configured column
        self.assertEqual(sheets.data['T'][1][3], 'keep')                       # Campaign status untouched

    def test_configured_column_missing_in_sheet_is_ignored_not_created(self):
        cfg = {**CFG, 'tracking_columns': {'replied_at': 'Replied at'}}
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'A', 'EMAIL_SENT', '', '', '']))
        writer_for(sheets, cfg).apply('T', 2, 'a@x.com', sp.RESPONDED, tracking_values={'replied_at': 'T9'})
        self.assertEqual(sheets.updates, [("'T'!C2", 'RESPONDED')])
        self.assertEqual(sheets.data['T'][0], HEADERS)  # no header invented

    def test_protected_columns_cannot_be_configured_as_tracking_columns(self):
        for header in ('Email', 'Merge status', 'Campaign status'):
            with self.assertRaises(ValueError):
                writer_for(FakeSheetsRW(tab_data()), {**CFG, 'tracking_columns': {'open_count': header}})
        with self.assertRaises(ValueError):
            writer_for(FakeSheetsRW(tab_data()), {**CFG, 'tracking_columns': {'bogus_metric': 'X'}})


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = make_store(self.clock)
        self.settings = TrackingSettings()

    def msg(self, to, row):
        return send(self.store, key=f'T|{to}', tab='T', row=row, to=to, gid=f'g-{to}', tid=f't-{to}')

    def test_each_kind_of_evidence_maps_to_one_status(self):
        sheets = FakeSheetsRW(tab_data(
            ['open@x.com', 'a', 'EMAIL_SENT', '', '', ''], ['reply@x.com', 'b', 'EMAIL_OPENED', '', '', ''],
            ['bounce@x.com', 'c', 'EMAIL_SENT', '', '', ''], ['temp@x.com', 'd', 'EMAIL_SENT', '', '', ''],
            ['plain@x.com', 'e', '', '', '', '']))
        m = {a: self.msg(f'{a}@x.com', i) for i, a in enumerate(['open', 'reply', 'bounce', 'temp', 'plain'], 2)}
        self.clock.advance(300)
        self.store.record_open(m['open']['tracking_id'], 'c', 'Mozilla')
        self.store.add_event(m['reply']['id'], 'reply', self.clock(), 'r1', {}, 'reply:r1')
        self.store.add_event(m['bounce']['id'], 'bounce', self.clock(), 'n1', {}, 'bounce:n1')
        self.store.add_event(m['temp']['id'], 'bounce_temp', self.clock(), 'n2', {}, 'bounce_temp:n2')
        w = writer_for(sheets)
        res = sync_statuses(self.store, w, self.settings, apply=True)
        self.assertEqual({r['to']: (r['desired'], r['result']) for r in res},
                         {'open@x.com': ('EMAIL_OPENED', 'WRITTEN'), 'reply@x.com': ('RESPONDED', 'WRITTEN'),
                          'bounce@x.com': ('BOUNCED', 'WRITTEN'), 'temp@x.com': ('BOUNCE_TEMPORARY', 'WRITTEN'),
                          'plain@x.com': ('EMAIL_SENT', 'WRITTEN')})
        self.assertEqual([r[2] for r in sheets.data['T'][1:]],
                         ['EMAIL_OPENED', 'RESPONDED', 'BOUNCED', 'BOUNCE_TEMPORARY', 'EMAIL_SENT'])

    def test_idempotent_and_preview_safe(self):
        sheets = FakeSheetsRW(tab_data(['open@x.com', 'a', 'EMAIL_SENT', '', '', '']))
        m = self.msg('open@x.com', 2)
        self.clock.advance(300)
        self.store.record_open(m['tracking_id'], 'c', 'Mozilla')
        w = writer_for(sheets)
        self.assertEqual(len(sync_statuses(self.store, w, self.settings, apply=False)), 1)
        self.assertEqual(sheets.updates, [])
        self.assertEqual(len(sync_statuses(self.store, w, self.settings, apply=True)), 1)
        self.assertEqual(sync_statuses(self.store, w, self.settings, apply=True), [])
        self.assertEqual(len(sheets.updates), 1)

    def test_no_regression_and_conflicts_are_not_retried_forever(self):
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'a', 'RESPONDED', '', '', ''], ['b@x.com', 'b', 'BOUNCED', '', '', '']))
        a, b = self.msg('a@x.com', 2), self.msg('b@x.com', 3)
        self.clock.advance(300)
        self.store.record_open(a['tracking_id'], 'c', 'Mozilla')               # open after a reply: no regress
        self.store.add_event(b['id'], 'reply', self.clock(), 'r1', {}, 'reply:r1')  # reply from a bounced address
        w = writer_for(sheets)
        res = sync_statuses(self.store, w, self.settings, apply=True)
        self.assertEqual({r['to']: r['result'] for r in res}, {'a@x.com': 'SKIPPED_STATUS', 'b@x.com': 'CONFLICT'})
        self.assertEqual([r[2] for r in sheets.data['T'][1:]], ['RESPONDED', 'BOUNCED'])
        self.assertEqual(sync_statuses(self.store, w, self.settings, apply=True), [])

    def test_suspect_opens_do_not_change_status_by_default(self):
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'a', 'EMAIL_SENT', '', '', '']))
        a = self.msg('a@x.com', 2)
        self.clock.advance(2)
        self.store.record_open(a['tracking_id'], 'c', 'Mozilla')  # 2s after sending: suspect
        w = writer_for(sheets)
        first = sync_statuses(self.store, w, self.settings, apply=True)  # records the baseline, writes nothing
        self.assertEqual([r['result'] for r in first], ['ALREADY_SET'])
        self.assertEqual(sheets.updates, [])
        self.assertEqual(sync_statuses(self.store, w, self.settings, apply=True), [])
        self.assertEqual(sheets.data['T'][1][2], 'EMAIL_SENT')
        loose = TrackingSettings(count_suspect_opens=True)
        res = sync_statuses(self.store, writer_for(sheets), loose, apply=True)
        self.assertEqual(res[0]['result'], 'WRITTEN')

    def test_failed_write_is_retried_next_sync(self):
        sheets = FakeSheetsRW(tab_data(['a@x.com', 'a', '', '', '', '']))
        self.msg('a@x.com', 2)  # Gmail accepted it, but the sheet never got EMAIL_SENT
        sheets.write_errors = [http_error(403)]
        w = writer_for(sheets)
        self.assertEqual(sync_statuses(self.store, w, self.settings, apply=True)[0]['result'], 'WRITE_FAILED')
        self.assertEqual(sheets.data['T'][1][2], '')
        self.assertEqual(sync_statuses(self.store, w, self.settings, apply=True)[0]['result'], 'WRITTEN')
        self.assertEqual(sheets.data['T'][1][2], 'EMAIL_SENT')


class FakeIngestGmail:
    def __init__(self, notices=(), threads=None):
        self.notices, self.threads = list(notices), threads or {}

    def find_bounces(self, days=45, max_messages=200):
        return self.notices

    def get_thread_messages(self, thread_id):
        return self.threads.get(thread_id, [])


def ms(dt):
    return int(dt.timestamp() * 1000)


def notice(nid, subject, addr, at):
    return {'id': nid, 'internalDate': ms(at), 'subject': subject, 'failed': addr, 'snippet': ''}


DSN_FAIL = 'Delivery Status Notification (Failure)'
DSN_DELAY = 'Delivery Status Notification (Delay)'


def tmsg(mid, at, frm='Sam <sam@x.com>', labels=('INBOX',), **headers):
    return {'id': mid, 'internalDate': ms(at), 'labelIds': list(labels), 'headers': {'from': frm, **headers}}


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = make_store(self.clock)
        self.a = send(self.store, key='T|sam@x.com', tab='T', row=2, to='sam@x.com', gid='gA', tid='thrA')
        self.later = T0 + timedelta(hours=1)

    def types(self):
        import sqlite3
        return [r[0] for r in sqlite3.connect(self.store.db_path).execute('SELECT type FROM events ORDER BY id')]

    def test_permanent_vs_temporary_bounce(self):
        g = FakeIngestGmail([notice('n1', DSN_FAIL, 'sam@x.com', self.later),
                             notice('n2', DSN_DELAY, 'sam@x.com', self.later)])
        res = ingest_bounces(self.store, g, apply=True)
        self.assertEqual(sorted(r['kind'] for r in res), ['permanent', 'temporary'])
        self.assertEqual(sorted(self.types()), ['bounce', 'bounce_temp'])
        row = self.store.message_rows()[0]
        self.assertEqual((row['bounces_perm'], row['bounces_temp']), (1, 1))

    def test_bounce_ingest_is_idempotent_and_previews_safely(self):
        g = FakeIngestGmail([notice('n1', DSN_FAIL, 'sam@x.com', self.later)])
        ingest_bounces(self.store, g, apply=False)
        self.assertEqual(self.types(), [])
        self.assertEqual(self.store.meta_get('last_bounce_ingest'), None)
        ingest_bounces(self.store, g, apply=True)
        ingest_bounces(self.store, g, apply=True)
        self.assertEqual(self.types(), ['bounce'])
        self.assertIsNotNone(self.store.meta_get('last_bounce_ingest'))

    def test_unrelated_or_older_notices_ignored(self):
        g = FakeIngestGmail([notice('n1', DSN_FAIL, 'stranger@y.com', self.later),
                             notice('n2', 'Weekly digest', 'sam@x.com', self.later),
                             notice('n3', DSN_FAIL, 'sam@x.com', T0 - timedelta(days=3))])  # before we sent
        self.assertEqual(ingest_bounces(self.store, g, apply=True), [])
        self.assertEqual(self.types(), [])

    def test_bounce_is_attached_to_the_most_recent_preceding_send(self):
        self.clock.advance(86400)
        b = send(self.store, key='U|sam@x.com', tab='U', row=5, to='sam@x.com', gid='gB', tid='thrB')
        g = FakeIngestGmail([notice('n1', DSN_FAIL, 'sam@x.com', T0 + timedelta(days=1, minutes=5))])
        ingest_bounces(self.store, g, apply=True)
        rows = {r['id']: r['bounces_perm'] for r in self.store.message_rows()}
        self.assertEqual(rows, {self.a['id']: 0, b['id']: 1})

    def test_human_reply_recorded_with_sender_time_thread(self):
        g = FakeIngestGmail(threads={'thrA': [tmsg('gA', T0, labels=('SENT',)),
                                              tmsg('r1', self.later, 'Sam Jones <Sam.Jones@x.com>')]})
        replies, no_thread = ingest_replies(self.store, g, apply=True)
        self.assertEqual((len(replies), no_thread), (1, 0))
        self.assertEqual((replies[0]['from'], replies[0]['thread'], replies[0]['at']),
                         ('Sam.Jones@x.com', 'thrA', self.later.isoformat()))
        self.assertEqual(self.store.message_rows()[0]['replies'], 1)
        self.assertEqual(self.store.blocked_reason('sam@x.com'), 'REPLIED')

    def test_automatic_and_own_and_older_messages_are_not_replies(self):
        g = FakeIngestGmail(threads={'thrA': [
            tmsg('gA', T0, labels=('SENT',)),
            tmsg('own', self.later, labels=('SENT',)),
            tmsg('ooo', self.later, subject='Out of Office: back Monday'),
            tmsg('auto', self.later, **{'auto-submitted': 'auto-replied'}),
            tmsg('md', self.later, frm='Mail Delivery Subsystem <mailer-daemon@googlemail.com>'),
            tmsg('bulk', self.later, precedence='bulk'),
            tmsg('old', T0 - timedelta(hours=1)),
        ]})
        replies, _ = ingest_replies(self.store, g, apply=True)
        self.assertEqual(replies, [])
        self.assertEqual(self.store.message_rows()[0]['replies'], 0)

    def test_reply_ingest_idempotent_preview_and_legacy(self):
        send(self.store, key='T|old@x.com', tab='T', row=3, to='old@x.com', gid='gL', tid=None)
        import sqlite3
        c = sqlite3.connect(self.store.db_path)
        c.execute("UPDATE messages SET gmail_thread_id=NULL WHERE recipient='old@x.com'")
        c.commit()
        g = FakeIngestGmail(threads={'thrA': [tmsg('r1', self.later)]})
        replies, no_thread = ingest_replies(self.store, g, apply=False)
        self.assertEqual((len(replies), no_thread, self.types()), (1, 1, []))
        ingest_replies(self.store, g, apply=True)
        ingest_replies(self.store, g, apply=True)
        self.assertEqual(self.types(), ['reply'])


class BounceKindTests(unittest.TestCase):
    def test_classify(self):
        known = {'a@x.com'}
        self.assertEqual(classify_notice({'subject': DSN_FAIL, 'failed': 'a@x.com'}, known), {'a@x.com': 'permanent'})
        self.assertEqual(classify_notice({'subject': DSN_DELAY, 'failed': 'a@x.com'}, known), {'a@x.com': 'temporary'})
        self.assertEqual(classify_notice({'subject': 'Hello', 'failed': 'a@x.com'}, known), {})

    def run_service(self, rows, notices, apply=True):
        data = {'T': [['Email', 'Merge status']] + rows}
        sheets = FakeSheetsRW(data)
        cfg = {**CFG, 'tabs': {'T': {'template': 'cold', 'email_header': 'Email'}}}
        svc = BounceService(sheets, FakeIngestGmail(notices), cfg, 'SID')
        return svc.check_all(apply=apply), sheets

    def test_temporary_bounce_only_marks_sent_rows(self):
        n = [notice('n1', DSN_DELAY, 'a@x.com', T0)]
        res, sheets = self.run_service([['a@x.com', 'EMAIL_SENT'], ['a@x.com', ''], ['a@x.com', 'EMAIL_OPENED']], n)
        self.assertEqual([r['result'] for r in res], ['MARKED', 'LEFT_ALONE_OTHER_STATUS', 'LEFT_ALONE_OTHER_STATUS'])
        self.assertEqual([r[1] for r in sheets.data['T'][1:]], ['BOUNCE_TEMPORARY', '', 'EMAIL_OPENED'])

    def test_permanent_wins_and_upgrades_temporary(self):
        n = [notice('n1', DSN_DELAY, 'a@x.com', T0), notice('n2', DSN_FAIL, 'a@x.com', T0)]
        res, sheets = self.run_service([['a@x.com', 'BOUNCE_TEMPORARY']], n)
        self.assertEqual(res[0]['result'], 'MARKED')
        self.assertEqual(sheets.data['T'][1][1], 'BOUNCED')

    def test_temporary_never_downgrades_a_permanent_bounce(self):
        res, sheets = self.run_service([['a@x.com', 'BOUNCED']], [notice('n1', DSN_DELAY, 'a@x.com', T0)])
        self.assertEqual(res[0]['result'], 'LEFT_ALONE_OTHER_STATUS')
        self.assertEqual(sheets.data['T'][1][1], 'BOUNCED')


class FakeGmail2:
    """Records sends, supports html_body, returns thread ids, can fail on demand."""

    def __init__(self):
        self.sent, self.errors = [], {}

    def send_email(self, to, subject, body, attachment_path=None, subtype='html', html_body=None):
        if self.errors.get(to):
            raise self.errors[to]
        self.sent.append(dict(to=to, subject=subject, body=body, attach=attachment_path, subtype=subtype,
                              html_body=html_body))
        n = len(self.sent)
        return {'id': f'm{n}', 'threadId': f't{n}'}


class OutreachIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dup = DuplicatePreventionService(os.path.join(self.tmp, 'dup.json'))
        self.trk = TrackingService(os.path.join(self.tmp, 'stats.json'))
        self.store = make_store(Clock())
        self.sheets = FakeSheetsRW(sheet_data())
        self.gmail = FakeGmail2()

    def make(self, track=None, send=True, sheets=None):
        sheets = sheets or self.sheets
        svc = OutreachService(sheets, self.gmail, EmailTemplateService(TPL), self.dup, self.trk, MAPPING, 'SID',
                              RESUME, send_enabled=send, store=self.store, writer=SheetStatusWriter(
                                  sheets, MAPPING, 'SID', sleep=lambda s: None), track=track)
        return svc

    def statuses(self, res):
        return {(r['tab'], r.get('row')): r['status'] for r in res}

    def test_default_sends_plain_only_and_records_everything(self):
        res = self.make().process_all()
        self.assertEqual(sum(r['status'] == 'SENT' for r in res), 4)
        for m in self.gmail.sent:
            self.assertIsNone(m['html_body'])
            self.assertEqual(m['subtype'], 'plain')
        rows = {r['recipient']: r for r in self.store.message_rows(state='sent')}
        self.assertEqual(set(rows), {'fay@f.com', 'cy@c.com', 'rae@r.com', 'fo@x.com'})
        fay = rows['fay@f.com']
        self.assertEqual((fay['tab'], fay['row_no'], fay['campaign'], fay['tracking_enabled']),
                         ('T-Founders', 2, 'T-Founders', 0))
        self.assertTrue(fay['gmail_message_id'] and fay['gmail_thread_id'] and fay['sent_at'])
        self.assertTrue(self.dup.is_sent('T-Founders|fay@f.com'))
        # only Merge status cells were written
        self.assertEqual(sorted(u[1] for u in self.sheets.updates), ['EMAIL_SENT'] * 4)

    def test_tracking_enabled_sends_exact_text_plus_tracked_html(self):
        track = TrackingSettings(enabled=True, base_url='https://t.example.com')
        res = self.make(track).process_all()
        self.assertTrue(all(r['tracking'] for r in res if r.get('subject')))
        fay = next(m for m in self.gmail.sent if m['to'] == 'fay@f.com')
        row = next(r for r in self.store.message_rows() if r['recipient'] == 'fay@f.com')
        self.assertEqual(row['tracking_enabled'], 1)
        self.assertIn(f'https://t.example.com/o/{row["tracking_id"]}.gif', fay['html_body'])
        self.assertNotIn('<', fay['body'])                     # the plain part is the exact template text
        self.assertTrue(fay['body'].startswith('Hi Fay,'))
        self.assertIn('Hi Fay,', fay['html_body'])
        self.assertEqual(row['link_count'], 1)                 # the signature's LinkedIn URL is the only link
        cy = next(m for m in self.gmail.sent if m['to'] == 'cy@c.com')
        cy_row = next(r for r in self.store.message_rows() if r['recipient'] == 'cy@c.com')
        self.assertEqual(cy_row['link_count'], 1)              # the cold template's LinkedIn link
        self.assertNotIn('linkedin.com/in/adarsharya2911/"', cy['html_body'])  # original URL not an href
        self.assertIn('https://t.example.com/c/', cy['html_body'])
        self.assertIn('https://www.linkedin.com/in/adarsharya2911/', cy['body'])  # plain text keeps the real URL

    def test_preview_creates_no_reservations_and_sends_nothing(self):
        res = self.make(send=False).process_all()
        self.assertEqual(sum(r['status'] == 'PREVIEW' for r in res), 4)
        self.assertEqual((self.store.message_rows(), self.gmail.sent, self.sheets.updates), ([], [], []))

    def test_second_run_never_resends(self):
        svc = self.make()
        svc.process_all()
        res = svc.process_all()
        self.assertEqual(len(self.gmail.sent), 4)
        self.assertNotIn('SENT', {r['status'] for r in res})

    def test_ambiguous_failure_parks_the_row_and_never_retries(self):
        self.gmail.errors['fay@f.com'] = RuntimeError('socket timed out')
        svc = self.make()
        res = svc.process_all()
        fay = next(r for r in res if r.get('to') == 'fay@f.com')
        self.assertEqual((fay['status'], fay['uncertain']), ('SEND_FAILED', True))
        self.assertEqual(self.store.get_message_by_key('T-Founders|fay@f.com')['state'], 'uncertain')
        self.gmail.errors.clear()
        res = svc.process_all()
        self.assertEqual(next(r for r in res if r.get('to') == 'fay@f.com')['status'],
                         'SKIP_UNCERTAIN_PREVIOUS_ATTEMPT')
        self.assertNotIn('fay@f.com', [m['to'] for m in self.gmail.sent])
        # a human confirms in Gmail that nothing went out -> the row becomes sendable again
        self.assertTrue(self.store.resolve('T-Founders|fay@f.com', 'unsent'))
        svc.process_all()
        self.assertIn('fay@f.com', [m['to'] for m in self.gmail.sent])

    def test_server_error_503_is_treated_as_ambiguous(self):
        self.gmail.errors['fay@f.com'] = http_error(503)
        self.make().process_all()
        self.assertEqual(self.store.get_message_by_key('T-Founders|fay@f.com')['state'], 'uncertain')

    def test_definite_rejections_release_the_reservation_for_retry(self):
        for err in (http_error(400), http_error(403), FileNotFoundError('resume')):
            store = make_store(Clock())
            self.store = store
            self.dup = DuplicatePreventionService(os.path.join(tempfile.mkdtemp(), 'd.json'))
            self.gmail = FakeGmail2()
            self.gmail.errors['fay@f.com'] = err
            svc = self.make(sheets=FakeSheetsRW(sheet_data()))
            res = svc.process_all()
            fay = next(r for r in res if r.get('to') == 'fay@f.com')
            self.assertEqual((fay['status'], fay['uncertain']), ('SEND_FAILED', False), err)
            self.assertIsNone(store.get_message_by_key('T-Founders|fay@f.com'))
            self.gmail.errors.clear()
            svc.process_all()
            self.assertIn('fay@f.com', [m['to'] for m in self.gmail.sent])

    def test_crash_after_reservation_blocks_resend(self):
        self.store.reserve_send('T-Founders|fay@f.com', 'T-Founders', 2, 'fay@f.com', 'c', 's', False)
        res = self.make().process_all()  # new process, previous one died before/after calling Gmail
        self.assertEqual(next(r for r in res if r.get('to') == 'fay@f.com')['status'],
                         'SKIP_UNCERTAIN_PREVIOUS_ATTEMPT')
        self.assertNotIn('fay@f.com', [m['to'] for m in self.gmail.sent])

    def test_gmail_ok_sheet_fails_then_reconciles_without_resending(self):
        self.sheets.fail_updates = True
        svc = self.make()
        res = svc.process_all()
        self.assertEqual(sum(r['status'] == 'SENT_STATUS_UPDATE_FAILED' for r in res), 4)
        self.assertEqual(len(self.gmail.sent), 4)
        self.assertEqual(self.store.get_message_by_key('T-Founders|fay@f.com')['state'], 'sent')
        self.sheets.fail_updates = False
        res = svc.process_all()
        self.assertEqual(sum(r['status'] == 'RECONCILED' for r in res), 4)
        self.assertEqual(len(self.gmail.sent), 4)
        self.assertEqual(sorted(u[1] for u in self.sheets.updates), ['EMAIL_SENT'] * 4)

    def test_recordings_failure_after_gmail_success_still_blocks_duplicates(self):
        def boom(*a, **k):
            raise RuntimeError('disk full')
        self.store.finalize_sent = boom
        svc = self.make()
        svc.process_all()
        self.assertEqual(len(self.gmail.sent), 4)
        svc.process_all()
        self.assertEqual(len(self.gmail.sent), 4)

    def blocked_data(self, other_status):
        data = sheet_data()
        # the same address (fo@x.com, pending in T-Follow) is RESPONDED/BOUNCED in another tab
        data['T-Cold'].append(['Fo', 'fo@x.com', 'Dev', 'FoCo', 'P', 'L', 'ach', 'thing', other_status])
        return data

    def test_replied_or_bounced_in_another_tab_blocks_every_tab(self):
        for status, expected in (('RESPONDED', 'SKIP_REPLIED_ELSEWHERE'), ('BOUNCED', 'SKIP_BOUNCED_ELSEWHERE')):
            self.dup = DuplicatePreventionService(os.path.join(tempfile.mkdtemp(), 'd.json'))
            self.gmail = FakeGmail2()
            sheets = FakeSheetsRW(self.blocked_data(status))
            res = self.make(sheets=sheets).process_all()
            follow = next(r for r in res if r.get('to') == 'fo@x.com' and r['tab'] == 'T-Follow')
            self.assertEqual(follow['status'], expected)
            self.assertNotIn('fo@x.com', [m['to'] for m in self.gmail.sent])

    def test_reply_recorded_in_tracking_db_blocks_other_tabs(self):
        fo = send(self.store, key='T-Other|fo@x.com', tab='T-Other', to='fo@x.com', gid='gX', tid='tX')
        self.store.add_event(fo['id'], 'reply', T0, 'r1', {}, 'reply:r1')
        res = self.make().process_all()
        self.assertEqual(next(r for r in res if r.get('to') == 'fo@x.com')['status'], 'SKIP_REPLIED_ELSEWHERE')
        self.assertNotIn('fo@x.com', [m['to'] for m in self.gmail.sent])

    def test_new_statuses_are_respected(self):
        data = sheet_data()
        data['T-Cold'][1].append('RESPONDED')   # the Merge status cell is the 9th column
        data['T-Recruit'][1][0] = 'BOUNCE_TEMPORARY'
        res = self.make(sheets=FakeSheetsRW(data)).process_all()
        st = self.statuses(res)
        self.assertEqual(st[('T-Cold', 2)], 'SKIP_RESPONDED')
        self.assertEqual(st[('T-Recruit', 2)], 'FLAG_BOUNCE_TEMPORARY')
        self.assertNotIn('cy@c.com', [m['to'] for m in self.gmail.sent])
        self.assertNotIn('rae@r.com', [m['to'] for m in self.gmail.sent])


class GmailHtmlTests(unittest.TestCase):
    def _send(self, **kw):
        captured = {}

        class Exec:
            def execute(self):
                return {'id': 'z', 'threadId': 'th'}

        class Msgs:
            def send(self, userId, body):
                captured['raw'] = body['raw']
                return Exec()

        class Users:
            def messages(self):
                return Msgs()

        class Svc:
            def users(self):
                return Users()

        g = object.__new__(GmailService)
        g.service = Svc()
        g.send_email('a@b.com', 'Subj — x', 'Hi — plain\nline2', **kw)
        return email.message_from_bytes(base64.urlsafe_b64decode(captured['raw']), policy=policy.default)

    def test_alternative_parts_and_attachment(self):
        m = self._send(attachment_path=RESUME, subtype='plain', html_body='<p>Hi — html<img src="https://t/o/x.gif"></p>')
        self.assertEqual(m['subject'], 'Subj — x')
        self.assertEqual(m.get_content_type(), 'multipart/mixed')
        alt = [p for p in m.walk() if p.get_content_type() == 'multipart/alternative']
        self.assertEqual(len(alt), 1)
        self.assertEqual(m.get_body(('plain',)).get_content().rstrip('\n'), 'Hi — plain\nline2')
        self.assertIn('https://t/o/x.gif', m.get_body(('html',)).get_content())
        self.assertEqual([p.get_filename() for p in m.walk() if p.get_filename()], ['resume.pdf'])

    def test_without_html_body_nothing_changes(self):
        m = self._send(subtype='plain')
        self.assertEqual(m.get_content_type(), 'multipart/mixed')
        self.assertEqual([p.get_content_type() for p in m.walk()], ['multipart/mixed', 'text/plain'])


if __name__ == '__main__':
    unittest.main()
