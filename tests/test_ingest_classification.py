"""Mocked tests for reply/bounce ingestion. Fake Gmail and fake Sheets only; no real mailbox is read."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from services import status_policy as sp
from services.bounce_service import classify_notice, failed_addresses, notice_kind
from services.event_ingest import CATEGORIES, ingest_all, ingest_bounces, ingest_replies
from services.gmail_service import SCOPES, SCOPES_WITH_READ, GmailService
from services.message_classifier import (AUTOMATED, BOUNCE_PERMANENT, BOUNCE_TEMPORARY, BOUNCE_UNCLASSIFIED, HUMAN,
                                         OUT_OF_OFFICE, bounce_kind, classify_message)
from services.status_sync import sync_statuses
from services.tracking_settings import TrackingSettings
from tests.test_tracking_core import T0, Clock, make_store, send
from tests.test_tracking_integration import (DSN_DELAY, DSN_FAIL, FakeSheetsRW, ms, tab_data, tmsg, writer_for)

LATER = T0 + timedelta(hours=1)
DAEMON = 'Mail Delivery Subsystem <mailer-daemon@googlemail.com>'


class FakeGmail3:
    def __init__(self, notices=(), threads=None, metas=None, thread_errors=()):
        self.notices, self.threads, self.metas = list(notices), threads or {}, metas or {}
        self.thread_errors, self.meta_calls = set(thread_errors), []

    def find_bounces(self, days=45, max_messages=200):
        return self.notices

    def get_thread_messages(self, thread_id):
        if thread_id in self.thread_errors:
            raise RuntimeError('gmail 500')
        return self.threads.get(thread_id, [])

    def get_message_meta(self, message_id):
        self.meta_calls.append(message_id)
        return self.metas.get(message_id)


def msg(mid, at, frm='Sam <sam@x.com>', snippet='', labels=('INBOX',), **headers):
    m = tmsg(mid, at, frm, labels, **headers)
    m['snippet'] = snippet
    return m


def event_rows(store):
    return sqlite3.connect(store.db_path).execute(
        'SELECT type, dedupe_key FROM events ORDER BY id').fetchall()


class ClassifierTests(unittest.TestCase):
    CASES = [
        # (headers, snippet, expected category)
        ({'from': 'Sam <sam@x.com>', 'subject': 'Re: Application for Junior SWE'}, '', HUMAN),
        ({'from': 'Sam <sam@x.com>', 'subject': 'Re: sorry, my last mail was undeliverable? thanks'}, '', HUMAN),
        ({'from': 'Sam <sam@x.com>', 'subject': 'Re: delay in hiring, warning: busy quarter'}, '', HUMAN),
        ({'from': DAEMON, 'subject': DSN_FAIL}, '', BOUNCE_PERMANENT),
        ({'from': DAEMON, 'subject': DSN_DELAY}, '', BOUNCE_TEMPORARY),
        ({'from': 'postmaster@corp.com', 'subject': 'Undeliverable: hi'}, '', BOUNCE_PERMANENT),
        ({'from': 'Mail Delivery System <MAILER-DAEMON@mx.x.com>',
          'subject': 'Mail delivery failed: returning message to sender'}, '', BOUNCE_PERMANENT),
        ({'from': 'MAILER-DAEMON@mx.x.com', 'subject': 'Warning: could not send message for past 4 hours'}, '',
         BOUNCE_TEMPORARY),
        ({'from': 'MAILER-DAEMON@mx.x.com', 'subject': 'Re: hi'}, '550 5.1.1 user unknown', BOUNCE_PERMANENT),
        ({'from': 'MAILER-DAEMON@mx.x.com', 'subject': 'Re: hi'}, '452 4.2.2 mailbox full', BOUNCE_TEMPORARY),
        ({'from': 'MAILER-DAEMON@mx.x.com', 'subject': 'Re: hi'}, 'something happened', BOUNCE_UNCLASSIFIED),
        ({'from': 'MAILER-DAEMON@mx.x.com', 'subject': 'Re: hi'}, '550 5.1.1 but also 4.2.2 retry', BOUNCE_UNCLASSIFIED),
        ({'from': 'x@y.com', 'subject': 'Report', 'content-type': 'multipart/report; report-type=delivery-status'},
         '5.1.1 bad mailbox', BOUNCE_PERMANENT),
        ({'from': 'Sam <sam@x.com>', 'subject': 'Automatic reply: Application', 'auto-submitted': 'auto-replied'}, '',
         OUT_OF_OFFICE),
        ({'from': 'Sam <sam@x.com>', 'subject': 'Out of Office: back Monday'}, '', OUT_OF_OFFICE),
        ({'from': 'Sam <sam@x.com>', 'subject': 'Re: hi', 'x-autoreply': 'yes'}, '', OUT_OF_OFFICE),
        ({'from': 'Sam <sam@x.com>', 'subject': 'On vacation until the 20th'}, '', OUT_OF_OFFICE),
        ({'from': 'x@y.com', 'subject': 'Deals', 'precedence': 'bulk'}, '', AUTOMATED),
        ({'from': 'x@y.com', 'subject': 'News', 'list-unsubscribe': '<mailto:u@y.com>'}, '', AUTOMATED),
        ({'from': 'x@y.com', 'subject': 'News', 'list-id': '<news.y.com>'}, '', AUTOMATED),
        ({'from': 'No Reply <no-reply@y.com>', 'subject': 'Your receipt'}, '', AUTOMATED),
        ({'from': 'x@y.com', 'subject': 'Ticket', 'auto-submitted': 'auto-generated'}, '', AUTOMATED),
        ({'from': 'x@y.com', 'subject': 'Hi', 'x-auto-response-suppress': 'All'}, '', AUTOMATED),
    ]

    def test_matrix(self):
        for headers, snippet, expected in self.CASES:
            self.assertEqual(classify_message(headers, snippet), expected, (headers, snippet))

    def test_a_failure_sounding_subject_from_a_person_is_still_a_human_reply(self):
        self.assertEqual(classify_message({'from': 'Sam <sam@x.com>', 'subject': 'Undeliverable: my reply'}), HUMAN)
        self.assertIsNone(bounce_kind('Delivery Status Notification (Failure)', 'Sam <sam@x.com>'))
        # ...unless the message itself is flagged as machine-generated
        self.assertEqual(bounce_kind('Delivery Status Notification (Failure)', 'Sam <sam@x.com>',
                                     auto_submitted='auto-replied'), 'permanent')

    def test_bounces_are_never_treated_as_out_of_office(self):
        h = {'from': DAEMON, 'subject': DSN_FAIL, 'auto-submitted': 'auto-replied'}  # real DSNs carry this header
        self.assertEqual(classify_message(h), BOUNCE_PERMANENT)

    def test_malformed_input_never_raises_and_defaults_to_human_only_if_nothing_is_automatic(self):
        for bad in ({}, None, {'from': None, 'subject': None}, {'from': 123, 'subject': ['x'], 'precedence': 5},
                    {'': ''}, {'subject': 'x' * 100000}):
            self.assertIn(classify_message(bad, None), (HUMAN, AUTOMATED))
        self.assertEqual(classify_message({'from': None, 'subject': None}, None), HUMAN)
        self.assertIsNone(bounce_kind(None, None, None, None))


class NoticeTests(unittest.TestCase):
    known = {'bad@x.com', 'ok@x.com'}

    def test_kinds(self):
        self.assertEqual(notice_kind({'subject': DSN_FAIL}), 'permanent')
        self.assertEqual(notice_kind({'subject': DSN_DELAY}), 'temporary')
        self.assertIsNone(notice_kind({'subject': 'Weekly digest'}))
        self.assertEqual(notice_kind({'subject': 'Re: x', 'from': DAEMON, 'snippet': '550 5.1.1 nope'}), 'permanent')

    def test_address_extraction_and_snippet_fallback(self):
        self.assertEqual(classify_notice({'subject': DSN_FAIL, 'failed': 'Bad@X.com'}, self.known),
                         {'bad@x.com': 'permanent'})
        self.assertEqual(classify_notice({'subject': DSN_DELAY, 'failed': '',
                                          'snippet': 'delayed delivery to bad@x.com'}, self.known),
                         {'bad@x.com': 'temporary'})
        self.assertEqual(classify_notice({'subject': DSN_FAIL, 'failed': 'stranger@y.com'}, self.known), {})
        self.assertEqual(failed_addresses({'subject': DSN_DELAY, 'failed': 'bad@x.com'}, self.known), set())

    def test_non_string_fields_do_not_crash(self):
        for n in ({}, {'subject': None}, {'subject': DSN_FAIL, 'failed': 5, 'snippet': None},
                  {'subject': DSN_FAIL, 'failed': ['a@b.com']}):
            classify_notice(n, self.known)


class IngestBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = make_store(self.clock)
        self.a = send(self.store, key='T|sam@x.com', tab='T', row=2, to='sam@x.com', gid='gA', tid='thrA')

    def counts(self, summary):
        return {c: summary['counts'].get(c, 0) for c in CATEGORIES}


class IngestCategoryTests(IngestBase):
    def mixed(self):
        return FakeGmail3(threads={'thrA': [
            msg('gA', T0, labels=('SENT',)),                                                # our own message
            msg('h1', LATER, snippet='Thanks, let us talk'),                                # human reply
            msg('h2', LATER + timedelta(hours=1), snippet='Also, a second line'),           # 2nd human: not another reply
            msg('ooo', LATER, subject='Automatic reply: away', **{'auto-submitted': 'auto-replied'}),
            msg('news', LATER, frm='News <news@x.com>', **{'list-unsubscribe': '<mailto:u@x.com>'}),
            msg('bnc1', LATER, frm=DAEMON, subject=DSN_FAIL),                               # permanent bounce
            msg('bnc2', LATER, frm=DAEMON, subject=DSN_DELAY),                              # temporary bounce
            msg('dmn', LATER, frm='MAILER-DAEMON@mx.x.com', subject='Re: hi', snippet='something'),
        ]})

    def test_counts_by_category_and_what_is_stored(self):
        s = ingest_all(self.store, self.mixed(), apply=True)
        self.assertEqual(self.counts(s), {'reply': 1, OUT_OF_OFFICE: 1, AUTOMATED: 1, BOUNCE_PERMANENT: 1,
                                          BOUNCE_TEMPORARY: 1, BOUNCE_UNCLASSIFIED: 1, 'bounce_unattributed': 0,
                                          'malformed': 0})
        self.assertEqual(sorted(t for t, _ in event_rows(self.store)), ['bounce', 'bounce_temp', 'reply'])
        row = self.store.message_rows()[0]
        self.assertEqual((row['replies'], row['bounces_perm'], row['bounces_temp']), (1, 1, 1))

    def test_auto_mail_is_never_a_reply_and_never_blocks_outreach(self):
        g = FakeGmail3(threads={'thrA': [msg('ooo', LATER, subject='Out of Office', **{'auto-submitted': 'auto-replied'}),
                                         msg('news', LATER, frm='N <n@x.com>', precedence='bulk'),
                                         msg('bnc2', LATER, frm=DAEMON, subject=DSN_DELAY)]})
        ingest_all(self.store, g, apply=True)
        self.assertEqual(self.store.message_rows()[0]['replies'], 0)
        self.assertIsNone(self.store.blocked_reason('sam@x.com'))  # OOO / bulk / temporary bounce: still contactable

    def test_human_reply_and_permanent_bounce_block_every_tab(self):
        g = FakeGmail3(threads={'thrA': [msg('h1', LATER)]})
        ingest_all(self.store, g, apply=True)
        self.assertEqual(self.store.blocked_reason('sam@x.com'), 'REPLIED')

    def test_messages_older_than_our_email_are_not_replies(self):
        g = FakeGmail3(threads={'thrA': [msg('old', T0 - timedelta(hours=2))]})
        self.assertEqual(self.counts(ingest_all(self.store, g, apply=True))['reply'], 0)

    def test_unattributed_bounce_notice_is_counted_not_stored(self):
        g = FakeGmail3(notices=[{'id': 'n1', 'internalDate': ms(LATER), 'subject': DSN_FAIL,
                                 'failed': 'stranger@y.com', 'snippet': ''}])
        s = ingest_all(self.store, g, apply=True)
        self.assertEqual(self.counts(s)['bounce_unattributed'], 1)
        self.assertEqual(event_rows(self.store), [])

    def test_notice_scan_attributes_threadless_bounces(self):
        g = FakeGmail3(notices=[{'id': 'n1', 'internalDate': ms(LATER), 'subject': DSN_DELAY,
                                 'failed': 'sam@x.com', 'snippet': ''}])
        s = ingest_all(self.store, g, apply=True)
        self.assertEqual(self.counts(s)[BOUNCE_TEMPORARY], 1)
        self.assertEqual([t for t, _ in event_rows(self.store)], ['bounce_temp'])


class IngestIdempotencyTests(IngestBase):
    def gmail(self):
        return FakeGmail3(threads={'thrA': [msg('h1', LATER), msg('bnc1', LATER, frm=DAEMON, subject=DSN_FAIL)]})

    def test_running_repeatedly_never_duplicates(self):
        g = self.gmail()
        first = ingest_all(self.store, g, apply=True)
        before = event_rows(self.store)
        self.assertEqual((first['new']['reply'], first['new'][BOUNCE_PERMANENT]), (1, 1))
        for _ in range(3):
            again = ingest_all(self.store, g, apply=True)
            self.assertEqual(sum(again['new'].values()), 0)
            self.assertEqual(self.counts(again)['reply'], 1)   # still *detected*, but already recorded
        self.assertEqual(event_rows(self.store), before)
        self.assertEqual(len(before), 2)

    def test_same_bounce_via_thread_and_via_notice_search_is_one_event(self):
        g = self.gmail()
        g.notices = [{'id': 'bnc1', 'internalDate': ms(LATER), 'subject': DSN_FAIL, 'failed': 'sam@x.com',
                      'snippet': ''}]
        s = ingest_all(self.store, g, apply=True)
        self.assertEqual(self.counts(s)[BOUNCE_PERMANENT], 1)
        self.assertEqual([t for t, _ in event_rows(self.store)].count('bounce'), 1)

    def test_preview_writes_nothing_and_flags_what_is_new(self):
        g = self.gmail()
        s = ingest_all(self.store, g, apply=False)
        self.assertEqual((event_rows(self.store), self.store.meta_get('last_reply_ingest')), ([], None))
        self.assertEqual((s['new']['reply'], s['applied']), (1, False))
        ingest_all(self.store, g, apply=True)
        again = ingest_all(self.store, g, apply=False)
        self.assertEqual(sum(again['new'].values()), 0)
        self.assertTrue(all(d['recorded'] for d in again['detections'] if d['category'] in ('reply', BOUNCE_PERMANENT)))

    def test_dedupe_uses_gmail_message_ids(self):
        ingest_all(self.store, self.gmail(), apply=True)
        keys = [k for _, k in event_rows(self.store)]
        self.assertEqual(sorted(keys), ['bounce:bnc1:sam@x.com', 'reply:h1'])

    def test_narrow_entry_points_remain_idempotent(self):
        g = self.gmail()
        r1, _ = ingest_replies(self.store, g, apply=True)
        r2, _ = ingest_replies(self.store, g, apply=True)
        self.assertEqual((len(r1), r1[0]['new'], r2[0]['new']), (1, True, False))
        g.notices = [{'id': 'n9', 'internalDate': ms(LATER), 'subject': DSN_DELAY, 'failed': 'sam@x.com', 'snippet': ''}]
        self.assertEqual([b['kind'] for b in ingest_bounces(self.store, g, apply=True)], ['temporary'])
        self.assertEqual([b['new'] for b in ingest_bounces(self.store, g, apply=True)], [False])


class LegacyRecoveryTests(unittest.TestCase):
    """Sends recorded before tracking existed have a Gmail message id but no thread id / timestamp."""

    def setUp(self):
        self.store = make_store(Clock())
        path = os.path.join(tempfile.mkdtemp(), 'tracking.json')
        with open(path, 'w') as f:
            json.dump({'T|old@x.com': 'gm-old', 'T|gone@x.com': 'gm-gone'}, f)
        self.store.import_legacy(path)
        self.g = FakeGmail3(
            metas={'gm-old': {'threadId': 'thrOld', 'internalDate': ms(T0)}},   # gm-gone: deleted -> None
            threads={'thrOld': [msg('r1', LATER, frm='Old <old@x.com>')]})

    def row(self, recipient):
        return next(r for r in self.store.message_rows() if r['recipient'] == recipient)

    def test_preview_finds_the_reply_but_backfills_nothing(self):
        s = ingest_all(self.store, self.g, apply=False)
        self.assertEqual((s['counts']['reply'], s['new']['reply'], s['backfilled']), (1, 1, 0))
        r = self.row('old@x.com')
        self.assertEqual((r['gmail_thread_id'], r['sent_at'], r['replies']), (None, None, 0))
        self.assertEqual(s['unresolved'], {'no_thread': 1})          # the deleted message

    def test_apply_backfills_only_empty_fields_once(self):
        s = ingest_all(self.store, self.g, apply=True)
        r = self.row('old@x.com')
        self.assertEqual((s['backfilled'], r['gmail_thread_id'], r['replies']), (1, 'thrOld', 1))
        self.assertEqual(r['sent_at'], T0.isoformat())
        calls = len(self.g.meta_calls)
        again = ingest_all(self.store, self.g, apply=True)
        self.assertEqual((again['backfilled'], again['new']['reply']), (0, 0))
        self.assertEqual(self.g.meta_calls[calls:], ['gm-gone'])       # only the unresolved one is looked up again

    def test_backfill_never_overwrites_existing_values(self):
        self.store.update_message_meta(self.row('old@x.com')['id'], thread_id='keep-me', sent_at='2026-01-01T00:00:00+00:00')
        self.store.update_message_meta(self.row('old@x.com')['id'], thread_id='other', sent_at='2030-01-01T00:00:00+00:00')
        r = self.row('old@x.com')
        self.assertEqual((r['gmail_thread_id'], r['sent_at']), ('keep-me', '2026-01-01T00:00:00+00:00'))


class MalformedAndErrorTests(IngestBase):
    def test_malformed_gmail_data_is_counted_never_raised_never_stored(self):
        g = FakeGmail3(
            notices=[None, 'text',
                     {'id': 'n1', 'subject': DSN_FAIL, 'failed': 'sam@x.com'},                       # no internalDate
                     {'id': 'n2', 'internalDate': ms(LATER), 'subject': DSN_FAIL, 'failed': '', 'snippet': 'none here'},
                     {'id': 'n3', 'internalDate': ms(LATER), 'subject': DSN_FAIL, 'failed': 12345, 'snippet': None},
                     {'id': 'n4', 'internalDate': 'garbage', 'subject': DSN_FAIL, 'failed': 'sam@x.com'}],
            threads={'thrA': [{'labelIds': []}, {'id': 't1', 'headers': {'from': 'x@y.com'}},
                              {'id': 't2', 'internalDate': 'garbage'}, None]})
        s = ingest_all(self.store, g, apply=True)
        self.assertEqual(s['counts']['malformed'], 10)
        self.assertEqual(event_rows(self.store), [])
        self.assertEqual(sum(v for k, v in s['counts'].items() if k != 'malformed'), 0)

    def test_thread_fetch_failure_is_isolated(self):
        b = send(self.store, key='T|bob@x.com', tab='T', row=3, to='bob@x.com', gid='gB', tid='thrB')
        g = FakeGmail3(threads={'thrB': [msg('h1', LATER, frm='Bob <bob@x.com>')]}, thread_errors={'thrA'})
        s = ingest_all(self.store, g, apply=True)
        self.assertEqual((s['unresolved'].get('thread_error'), s['counts']['reply']), (1, 1))
        self.assertEqual([r['recipient'] for r in self.store.message_rows() if r['replies']], ['bob@x.com'])


class ProposedStatusTests(unittest.TestCase):
    """Ingest preview -> proposed sheet changes under the status policy; the sheet is never written."""

    def setUp(self):
        self.clock = Clock()
        self.store = make_store(self.clock)
        self.cases = {}
        for i, (name, sheet_status) in enumerate([('open', 'EMAIL_OPENED'), ('openbnc', 'EMAIL_OPENED'),
                                                  ('bncreply', 'BOUNCED'), ('respdelay', 'RESPONDED'),
                                                  ('sentdelay', 'EMAIL_SENT'), ('sentreply', 'EMAIL_SENT')], start=2):
            self.cases[name] = (send(self.store, key=f'T|{name}@x.com', tab='T', row=i, to=f'{name}@x.com',
                                     gid=f'g{i}', tid=f't{i}'), sheet_status)
        self.sheets = FakeSheetsRW(tab_data(*[[f'{n}@x.com', n, st, 'campaign-keep', '', ''] for n, (_, st) in self.cases.items()]))
        self.writer = writer_for(self.sheets)

    def overlay(self, **by_name):
        return {self.cases[n][0]['id']: {'replies': 0, 'bounces_perm': 0, 'bounces_temp': 0, **v}
                for n, v in by_name.items()}

    def results(self, overlay):
        res = sync_statuses(self.store, self.writer, TrackingSettings(), apply=False, overlay=overlay)
        return {r['to'].split('@')[0]: (r['desired'], r['result']) for r in res}

    def test_precedence_applies_to_pending_evidence(self):
        got = self.results(self.overlay(open={'replies': 1}, openbnc={'bounces_perm': 1}, bncreply={'replies': 1},
                                        respdelay={'bounces_temp': 1}, sentdelay={'bounces_temp': 1},
                                        sentreply={'replies': 1}))
        self.assertEqual(got['open'], ('RESPONDED', 'WOULD_WRITE'))             # reply beats an open
        self.assertEqual(got['openbnc'], ('BOUNCED', 'CONFLICT'))               # contradictory: flagged, not written
        self.assertEqual(got['bncreply'], ('RESPONDED', 'CONFLICT'))            # reply from a bounced address: flagged
        self.assertEqual(got['respdelay'], ('BOUNCE_TEMPORARY', 'SKIPPED_STATUS'))  # lower never beats RESPONDED
        self.assertEqual(got['sentdelay'], ('BOUNCE_TEMPORARY', 'WOULD_WRITE'))
        self.assertEqual(got['sentreply'], ('RESPONDED', 'WOULD_WRITE'))

    def test_preview_changes_neither_sheet_nor_database(self):
        self.results(self.overlay(open={'replies': 1}, sentreply={'replies': 1}))
        self.assertEqual((self.sheets.updates, self.sheets.batches), ([], []))
        self.assertEqual(event_rows(self.store), [])
        self.assertEqual([r['sheet_status'] for r in self.store.message_rows()], [None] * 6)

    def test_overlay_is_ignored_when_applying(self):
        res = sync_statuses(self.store, self.writer, TrackingSettings(), apply=True,
                            overlay=self.overlay(sentreply={'replies': 1}))
        self.assertNotIn('RESPONDED', [r['desired'] for r in res])      # only recorded events can reach the sheet
        self.assertEqual(self.sheets.data['T'][6][2], 'EMAIL_SENT')

    def test_end_to_end_ingest_then_sync_never_regresses_a_status(self):
        g = FakeGmail3(threads={f't{i}': [msg(f'r{i}', LATER, frm=f'x <{n}@x.com>')]
                                for i, n in zip(range(2, 8), self.cases)})
        s = ingest_all(self.store, g, apply=False)
        proposed = self.results(s['overlay'])
        self.assertEqual({k: v[1] for k, v in proposed.items() if v[0] == 'RESPONDED'},
                         {'open': 'WOULD_WRITE', 'openbnc': 'WOULD_WRITE', 'bncreply': 'CONFLICT',
                          'respdelay': 'ALREADY_SET', 'sentdelay': 'WOULD_WRITE', 'sentreply': 'WOULD_WRITE'})
        ingest_all(self.store, g, apply=True)
        self.assertEqual((self.sheets.updates, self.sheets.batches), ([], []))   # ingest --apply never touches the sheet
        sync_statuses(self.store, self.writer, TrackingSettings(), apply=True)
        final = {r[1]: r[2] for r in self.sheets.data['T'][1:]}
        self.assertEqual(final['bncreply'], 'BOUNCED')       # conflict left alone
        self.assertEqual(final['respdelay'], 'RESPONDED')
        self.assertEqual(final['open'], 'RESPONDED')
        self.assertEqual({r[1]: r[3] for r in self.sheets.data['T'][1:]}['open'], 'campaign-keep')  # other columns intact


class ScopeConsentTests(unittest.TestCase):
    """gmail.readonly is requested only when a command needs it, and consent is re-asked only if missing."""

    def make(self, granted):
        d = tempfile.mkdtemp()
        token = os.path.join(d, 'token.json')
        with open(token, 'w') as f:
            json.dump({'scopes': granted}, f)
        flow_creds = mock.MagicMock(valid=True)
        flow_creds.to_json.return_value = json.dumps({'scopes': list(SCOPES_WITH_READ)})
        with mock.patch('services.gmail_service.Credentials') as C, \
                mock.patch('services.gmail_service.InstalledAppFlow') as F, \
                mock.patch('services.gmail_service.build') as B:
            C.from_authorized_user_file.return_value = mock.MagicMock(valid=True)
            F.from_client_secrets_file.return_value.run_local_server.return_value = flow_creds
            GmailService('credentials.json', token, scopes=self.scopes)
        return F, token

    scopes = SCOPES_WITH_READ

    def test_missing_readonly_scope_triggers_a_fresh_consent_for_exactly_the_needed_scopes(self):
        F, token = self.make(SCOPES)
        F.from_client_secrets_file.assert_called_once()
        self.assertEqual(F.from_client_secrets_file.call_args[0][1], list(SCOPES_WITH_READ))
        self.assertIn('gmail.readonly', open(token).read())

    def test_existing_grant_is_reused_without_prompting(self):
        F, _ = self.make(SCOPES_WITH_READ)
        F.from_client_secrets_file.assert_not_called()

    def test_send_only_commands_never_ask_for_read_access(self):
        self.scopes = SCOPES
        try:
            F, _ = self.make(SCOPES)
            F.from_client_secrets_file.assert_not_called()
            F, _ = self.make(SCOPES_WITH_READ)       # a wider token is fine for narrower commands
            F.from_client_secrets_file.assert_not_called()
        finally:
            self.scopes = SCOPES_WITH_READ

    def test_scope_list_is_minimal(self):
        self.assertEqual([s.rsplit('/', 1)[-1] for s in SCOPES_WITH_READ], ['gmail.send', 'spreadsheets', 'gmail.readonly'])


class NoticeAttributionTests(unittest.TestCase):
    """Regression: a bounce notice must never be pinned to a send that happened AFTER it."""

    def setUp(self):
        self.store = make_store(Clock())
        path = os.path.join(tempfile.mkdtemp(), 'tracking.json')
        with open(path, 'w') as f:
            json.dump({'T|old@x.com': 'gm-old', 'T|unk@x.com': 'gm-unk'}, f)   # recorded without send times
        self.store.import_legacy(path)
        n = lambda nid, addr, at: {'id': nid, 'internalDate': ms(at), 'subject': DSN_FAIL, 'failed': addr, 'snippet': ''}
        self.g = FakeGmail3(
            metas={'gm-old': {'threadId': 'thrOld', 'internalDate': ms(T0)}},     # gm-unk: send time unknowable
            threads={'thrOld': []},
            notices=[n('nOld', 'old@x.com', T0 - timedelta(days=2)),              # before our send: not ours
                     n('nNew', 'old@x.com', LATER),                               # after our send: ours
                     n('nUnk', 'unk@x.com', LATER)])                              # cannot verify order: not attributed

    def check(self, apply):
        s = ingest_all(self.store, self.g, apply=apply)
        self.assertEqual((s['counts'][BOUNCE_PERMANENT], s['counts']['bounce_unattributed']), (1, 2))
        self.assertEqual([d['source_id'] for d in s['detections'] if d['category'] == BOUNCE_PERMANENT], ['nNew'])
        return s

    def test_preview_uses_the_real_send_time_even_though_nothing_is_backfilled(self):
        self.check(apply=False)
        self.assertEqual(event_rows(self.store), [])

    def test_apply_records_only_the_notice_that_came_after_the_send(self):
        self.check(apply=True)
        self.assertEqual([k for _, k in event_rows(self.store)], ['bounce:nNew:old@x.com'])

    def test_preview_and_apply_agree(self):
        a = self.check(apply=False)
        b = self.check(apply=True)
        self.assertEqual(sorted(d['key'] for d in a['detections']), sorted(d['key'] for d in b['detections']))


class SyncScopeTests(unittest.TestCase):
    def test_messages_outside_the_spreadsheet_are_not_synced(self):
        store = make_store(Clock())
        send(store, key='TEST|me@x.com|ab12', tab='TEST', to='me@x.com', gid='gT', tid='tT')
        sheets = FakeSheetsRW(tab_data())
        self.assertEqual(sync_statuses(store, writer_for(sheets), TrackingSettings(), apply=False), [])


class CliTests(unittest.TestCase):
    """`run.py track ingest` end to end with a fake Gmail and a fake Sheet (no network, no mailbox)."""

    def run_cli(self, apply):
        import contextlib
        import io
        import argparse
        import main
        from tests.test_tracking_integration import CFG
        store = make_store(Clock())
        send(store, key='T|sam@x.com', tab='T', row=2, to='sam@x.com', gid='gA', tid='thrA')
        send(store, key='T|bob@x.com', tab='T', row=3, to='bob@x.com', gid='gB', tid='thrB')
        gmail = FakeGmail3(threads={'thrA': [msg('h1', LATER), msg('ooo', LATER, subject='Out of Office')],
                                    'thrB': [msg('bn', LATER, frm=DAEMON, subject=DSN_FAIL)]})
        gmail.creds = object()   # the real service exposes OAuth credentials here
        sheets = FakeSheetsRW(tab_data(['sam@x.com', 'Sam', 'EMAIL_OPENED', 'keep', '', ''],
                                       ['bob@x.com', 'Bob', 'EMAIL_SENT', 'keep', '', '']))
        args = argparse.Namespace(sub='ingest', apply=apply, days=45)
        out = io.StringIO()
        with mock.patch('main.build_store', return_value=(store, TrackingSettings())),                 mock.patch('services.gmail_service.GmailService', return_value=gmail) as G,                 mock.patch('services.google_sheets_service.GoogleSheetsService', return_value=sheets),                 mock.patch('services.outreach_service.load_mapping', return_value=CFG),                 contextlib.redirect_stdout(out):
            main.handle_track(args)
        return out.getvalue(), store, sheets, G

    def test_preview_output(self):
        text, store, sheets, G = self.run_cli(apply=False)
        self.assertIn('[PREVIEW - nothing recorded]', text)
        self.assertRegex(text, r'reply\s+1\s+\(1 not yet recorded\)')
        self.assertRegex(text, r'out_of_office\s+1')
        self.assertRegex(text, r'bounce_permanent\s+1\s+\(1 not yet recorded\)')
        self.assertIn('Proposed Merge status changes (NOTHING written to the sheet)', text)
        self.assertIn('EMAIL_OPENED -> RESPONDED', text)
        self.assertIn('EMAIL_SENT -> BOUNCED', text)
        self.assertIn('from detected-but-unrecorded events', text)
        self.assertEqual((event_rows(store), sheets.updates, sheets.batches), ([], [], []))
        self.assertEqual(G.call_args.kwargs['scopes'], SCOPES_WITH_READ)

    def test_apply_output_records_locally_and_still_leaves_the_sheet_alone(self):
        text, store, sheets, _ = self.run_cli(apply=True)
        self.assertIn('(recorded)', text)
        self.assertIn('Events recorded locally', text)
        self.assertEqual(sorted(t for t, _ in event_rows(store)), ['bounce', 'reply'])
        self.assertEqual((sheets.updates, sheets.batches), ([], []))
        self.assertEqual(sheets.data['T'][1][2:4], ['EMAIL_OPENED', 'keep'])


if __name__ == '__main__':
    unittest.main()
