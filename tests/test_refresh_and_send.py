"""Refresh-before-send and the scheduled refresh job. Fake Gmail + fake Sheets only: nothing is sent."""
import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from googleapiclient.errors import HttpError  # noqa: F401  (used through http_error)

from services.duplicate_prevention_service import DuplicatePreventionService
from services.email_template_service import EmailTemplateService
from services.gmail_service import SCOPES, AuthRequired, GmailService
from services.outreach_service import OutreachService
from services.refresh import RefreshError, run_refresh_job, sanitize, send_campaign
from services.run_lock import RunBusy, RunLock
from services.status_sync import SheetStatusWriter
from services.tracking_service import TrackingService
from services.tracking_settings import TrackingSettings
from services.tracking_store import TrackingStore
from tests.test_ingest_classification import DAEMON, LATER, msg
from tests.test_outreach import MAPPING, RESUME, TPL, sheet_data
from tests.test_tracking_core import T0, Clock, make_store, send
from tests.test_tracking_integration import DSN_FAIL, FakeGmail2, FakeSheetsRW, http_error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = TrackingSettings()


class CampaignGmail(FakeGmail2):
    """Send recorder (FakeGmail2) + the read methods the refresh uses."""

    def __init__(self, notices=(), threads=None, fail_notices=False, thread_errors=(), on_send=None, delay=0.0):
        super().__init__()
        self.notices, self.threads = list(notices), threads or {}
        self.fail_notices, self.thread_errors = fail_notices, set(thread_errors)
        self.on_send, self.delay, self.attempts = on_send, delay, []

    def send_email(self, to, subject, body, attachment_path=None, subtype='html', html_body=None):
        self.attempts.append(to)
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.on_send:
            self.on_send(to)
        return super().send_email(to, subject, body, attachment_path, subtype, html_body)

    def find_bounces(self, days=45, max_messages=200):
        if self.fail_notices:
            raise RuntimeError('gmail unavailable')
        return self.notices

    def get_thread_messages(self, thread_id):
        if thread_id in self.thread_errors:
            raise RuntimeError('gmail 500')
        return self.threads.get(thread_id, [])

    def get_message_meta(self, message_id):
        return None


def data_with_earlier_send(status='EMAIL_SENT'):
    """The usual four tabs, plus fo@x.com already emailed in T-Recruit (row 3) with `status`."""
    data = sheet_data()
    data['T-Recruit'].append([status, 'Fo', 'fo@x.com', 'FoCo', 'Dev', '5', 'Py', 'SQL', 'Goa', 'Prev', 'ach'])
    return data


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lock = os.path.join(self.tmp, 'run.lock')
        self.log = os.path.join(self.tmp, 'refresh.log')
        self.store = make_store(Clock())
        self.dup = DuplicatePreventionService(os.path.join(self.tmp, 'dup.json'))
        self.trk = TrackingService(os.path.join(self.tmp, 'stats.json'))

    def outreach(self, sheets, gmail, store=None, dup=None, revalidate=True):
        store = store or self.store
        writer = SheetStatusWriter(sheets, MAPPING, 'SID', sleep=lambda s: None)
        return OutreachService(sheets, gmail, EmailTemplateService(TPL), dup or self.dup, self.trk, MAPPING, 'SID',
                               RESUME, send_enabled=True, store=store, writer=writer, track=SETTINGS,
                               revalidate=revalidate)

    def earlier_send(self, thread_msgs, status='EMAIL_SENT'):
        """fo@x.com was emailed earlier from T-Recruit; `thread_msgs` are what Gmail now shows in that thread."""
        send(self.store, key='T-Recruit|fo@x.com', tab='T-Recruit', row=3, to='fo@x.com', gid='gFo', tid='thrFo')
        sheets = FakeSheetsRW(data_with_earlier_send(status))
        gmail = CampaignGmail(threads={'thrFo': [msg('gFo', T0, labels=('SENT',))] + thread_msgs})
        return sheets, gmail

    def run_send(self, outreach, **kw):
        return send_campaign(outreach, self.lock, self.log, **kw)

    def sent_to(self, gmail):
        return sorted(m['to'] for m in gmail.sent)


# ----------------------------------------------------------------------------------------------------
class SuppressionTests(Env):
    def test_reply_found_by_the_refresh_suppresses_that_address_in_every_tab(self):
        sheets, gmail = self.earlier_send([msg('r1', LATER, frm='Fo <fo@x.com>')])
        results = self.run_send(self.outreach(sheets, gmail))
        self.assertEqual(self.sent_to(gmail), ['cy@c.com', 'fay@f.com', 'rae@r.com'])   # fo@x.com NOT emailed
        self.assertEqual(sheets.data['T-Recruit'][2][0], 'RESPONDED')                    # refresh synced the reply
        status = {(r['tab'], r.get('to')): r['status'] for r in results}
        self.assertEqual(status[('T-Follow', 'fo@x.com')], 'SKIP_REPLIED_ELSEWHERE')
        self.assertEqual(self.store.blocked_reason('fo@x.com'), 'REPLIED')

    def test_permanent_bounce_found_by_the_refresh_suppresses_that_address_in_every_tab(self):
        sheets, gmail = self.earlier_send([msg('bn', LATER, frm=DAEMON, subject=DSN_FAIL)])
        results = self.run_send(self.outreach(sheets, gmail))
        self.assertNotIn('fo@x.com', self.sent_to(gmail))
        self.assertEqual(sheets.data['T-Recruit'][2][0], 'BOUNCED')
        self.assertEqual(next(r for r in results if r['tab'] == 'T-Follow')['status'], 'SKIP_BOUNCED_ELSEWHERE')

    def test_out_of_office_and_automated_mail_do_not_suppress(self):
        sheets, gmail = self.earlier_send([
            msg('ooo', LATER, frm='Fo <fo@x.com>', subject='Out of Office', **{'auto-submitted': 'auto-replied'}),
            msg('news', LATER, frm='N <n@x.com>', precedence='bulk')])
        self.run_send(self.outreach(sheets, gmail))
        self.assertIn('fo@x.com', self.sent_to(gmail))

    def test_other_suppression_statuses_block_across_tabs(self):
        for status in ('UNSUBSCRIBED', 'DO_NOT_TRACK', 'EMAIL_REPLIED', 'EMAIL_BOUNCED', 'NO_RECIPIENT'):
            sheets = FakeSheetsRW(data_with_earlier_send(status))
            gmail = CampaignGmail()
            res = self.outreach(sheets, gmail, store=make_store(Clock()),
                                dup=DuplicatePreventionService(os.path.join(tempfile.mkdtemp(), 'd.json'))).process_all()
            follow = next(r for r in res if r['tab'] == 'T-Follow')
            self.assertTrue(follow['status'].startswith('SKIP_'), (status, follow['status']))
            self.assertNotIn('fo@x.com', self.sent_to(gmail), status)

    def test_own_row_suppression_statuses_get_clear_labels(self):
        data = sheet_data()
        data['T-Recruit'][1][0] = 'UNSUBSCRIBED'
        data['T-Founders'][1][0] = 'DO_NOT_TRACK'
        res = self.outreach(FakeSheetsRW(data), CampaignGmail()).process_all()
        got = {(r['tab'], r['row']): r['status'] for r in res}
        self.assertEqual(got[('T-Recruit', 2)], 'SKIP_UNSUBSCRIBED')
        self.assertEqual(got[('T-Founders', 2)], 'SKIP_DO_NOT_TRACK')

    def test_a_temporary_bounce_elsewhere_does_not_suppress(self):
        sheets = FakeSheetsRW(data_with_earlier_send('BOUNCE_TEMPORARY'))
        gmail = CampaignGmail()
        self.outreach(sheets, gmail).process_all()
        self.assertIn('fo@x.com', self.sent_to(gmail))


# ----------------------------------------------------------------------------------------------------
class RefreshFailsClosedTests(Env):
    """If the refresh cannot fully complete, NOTHING is sent (and nothing is reserved)."""

    def assert_nothing_sent(self, sheets, gmail, before_messages):
        self.assertEqual((gmail.sent, gmail.attempts), ([], []))
        self.assertEqual(len(self.store.message_rows()), before_messages)       # no reservations created
        self.assertEqual([u for u in sheets.updates if u[1] == 'EMAIL_SENT'], [])

    def start(self, **gmail_kw):
        send(self.store, key='T-Recruit|fo@x.com', tab='T-Recruit', row=3, to='fo@x.com', gid='gFo', tid='thrFo')
        sheets = FakeSheetsRW(data_with_earlier_send())
        gmail = CampaignGmail(threads={'thrFo': [msg('r1', LATER, frm='Fo <fo@x.com>')]}, **gmail_kw)
        return sheets, gmail, len(self.store.message_rows())

    def test_gmail_unavailable(self):
        sheets, gmail, n = self.start(fail_notices=True)
        with self.assertRaisesRegex(RefreshError, 'ingest failed'):
            self.run_send(self.outreach(sheets, gmail))
        self.assert_nothing_sent(sheets, gmail, n)

    def test_a_thread_that_cannot_be_read_makes_the_refresh_incomplete(self):
        sheets, gmail, n = self.start(thread_errors={'thrFo'})
        with self.assertRaisesRegex(RefreshError, 'incomplete'):
            self.run_send(self.outreach(sheets, gmail))
        self.assert_nothing_sent(sheets, gmail, n)

    def test_sheet_write_failure_during_sync(self):
        sheets, gmail, n = self.start()
        sheets.write_errors = [http_error(403)]
        with self.assertRaisesRegex(RefreshError, 'sheet sync incomplete'):
            self.run_send(self.outreach(sheets, gmail))
        self.assert_nothing_sent(sheets, gmail, n)

    def test_sheet_unreadable_during_sync(self):
        sheets, gmail, n = self.start()

        def boom(sheet_id, tab):
            raise RuntimeError('sheets down')
        sheets.get_tab_values = boom
        with self.assertRaisesRegex(RefreshError, 'sheet sync failed'):
            self.run_send(self.outreach(sheets, gmail))
        self.assert_nothing_sent(sheets, gmail, n)

    def test_missing_pieces_fail_closed(self):
        sheets, gmail, _ = self.start()
        o = self.outreach(sheets, gmail)
        o.writer = None
        with self.assertRaises(RefreshError):
            self.run_send(o)
        self.assertEqual(gmail.sent, [])

    def test_recovers_once_the_cause_is_fixed_and_the_lock_is_always_released(self):
        sheets, gmail, n = self.start(fail_notices=True)
        o = self.outreach(sheets, gmail)
        with self.assertRaises(RefreshError):
            self.run_send(o)
        probe = RunLock(self.lock)
        self.assertTrue(probe.acquire())                    # the failed run did not leave the lock behind
        probe.release()
        gmail.fail_notices = False
        self.run_send(o)
        self.assertIn('fay@f.com', self.sent_to(gmail))


# ----------------------------------------------------------------------------------------------------
class RevalidationTests(Env):
    def test_status_written_since_the_tab_was_read_stops_the_send(self):
        sheets = FakeSheetsRW(sheet_data())
        sheets.fresh_override[('T-Founders', 2)] = ['RESPONDED', 'Fay', 'fay@f.com', 'FCo', 'Engineer', 'Delhi', 'a', 'b']
        gmail = CampaignGmail()
        res = self.outreach(sheets, gmail).process_all()
        self.assertEqual(next(r for r in res if r.get('to') == 'fay@f.com')['status'], 'SKIP_RESPONDED')
        self.assertNotIn('fay@f.com', self.sent_to(gmail))
        self.assertIn('cy@c.com', self.sent_to(gmail))     # unaffected recipients still go out

    def test_unknown_new_status_also_stops_the_send(self):
        sheets = FakeSheetsRW(sheet_data())
        sheets.fresh_override[('T-Founders', 2)] = ['WEIRD', 'Fay', 'fay@f.com', 'FCo', 'Engineer', 'Delhi', 'a', 'b']
        res = self.outreach(sheets, CampaignGmail()).process_all()
        self.assertEqual(next(r for r in res if r.get('to') == 'fay@f.com')['status'], 'SKIP_STATUS_CHANGED')

    def test_row_that_now_holds_a_different_address_is_not_sent(self):
        sheets = FakeSheetsRW(sheet_data())
        sheets.fresh_override[('T-Founders', 2)] = ['', 'Zed', 'zed@f.com', 'ZCo', 'Engineer', 'Delhi', 'a', 'b']
        gmail = CampaignGmail()
        res = self.outreach(sheets, gmail).process_all()
        self.assertEqual(next(r for r in res if r.get('to') == 'fay@f.com')['status'], 'SKIP_ROW_CHANGED')
        self.assertEqual([m for m in gmail.sent if m['to'] in ('fay@f.com', 'zed@f.com')], [])

    def test_unreadable_row_fails_closed(self):
        class Flaky(FakeSheetsRW):
            def get_sheet_data(self, sheet_id, range_name):
                if "'T-Founders'!2:2" in range_name:
                    raise RuntimeError('sheets down')
                return super().get_sheet_data(sheet_id, range_name)
        gmail = CampaignGmail()
        res = self.outreach(Flaky(sheet_data()), gmail).process_all()
        self.assertEqual(next(r for r in res if r.get('to') == 'fay@f.com')['status'], 'SKIP_REVALIDATION_FAILED')
        self.assertNotIn('fay@f.com', self.sent_to(gmail))
        self.assertEqual(self.store.get_message_by_key('T-Founders|fay@f.com'), None)

    def test_evidence_recorded_mid_run_blocks_later_recipients(self):
        other = send(self.store, key='T-Other|cy@c.com', tab='T-Other', to='cy@c.com', gid='gC', tid='thrC')
        sheets, state = FakeSheetsRW(sheet_data()), {}

        def on_send(to):  # while fay is being emailed, a reply from cy arrives and is recorded
            if to == 'fay@f.com':
                self.store.add_event(other['id'], 'reply', T0, 'rX', {}, 'reply:rX')
        gmail = CampaignGmail(on_send=on_send)
        res = self.outreach(sheets, gmail).process_all()
        self.assertIn('fay@f.com', self.sent_to(gmail))
        self.assertNotIn('cy@c.com', self.sent_to(gmail))
        self.assertEqual(next(r for r in res if r.get('to') == 'cy@c.com')['status'], 'SKIP_REPLIED_ELSEWHERE')

    def test_the_check_is_on_for_real_sends_in_the_cli(self):
        src = open(os.path.join(ROOT, 'main.py'), encoding='utf-8').read()
        self.assertIn('revalidate=send', src)
        self.assertIn('send_campaign(', src)
        self.assertNotIn('outreach.process_all()', src.split("elif args.mode == 'send':")[1].split("elif args.mode == 'refresh':")[0])


# ----------------------------------------------------------------------------------------------------
class RunLockTests(unittest.TestCase):
    def test_exclusive_and_released(self):
        p = os.path.join(tempfile.mkdtemp(), 'x.lock')
        a, b = RunLock(p), RunLock(p)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_context_manager_raises_when_busy(self):
        p = os.path.join(tempfile.mkdtemp(), 'x.lock')
        with RunLock(p):
            with self.assertRaises(RunBusy):
                with RunLock(p):
                    pass
        with RunLock(p):
            pass

    def test_other_process_cannot_take_it_and_a_crashed_holder_does_not_block_forever(self):
        p = os.path.join(tempfile.mkdtemp(), 'x.lock')
        code = ("import sys,time;sys.path.insert(0,%r);from services.run_lock import RunLock;"
                "l=RunLock(%r);print('held' if l.acquire() else 'busy',flush=True);time.sleep(120)") % (ROOT, p)
        child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'held')
            mine = RunLock(p)
            self.assertFalse(mine.acquire())
            self.assertEqual(mine.holder().get('pid'), child.pid)
            child.kill()                       # simulate a crash / power loss
            child.wait(timeout=10)
            self.assertTrue(mine.acquire(timeout=5))   # the OS freed it: no stale lock
            mine.release()
        finally:
            if child.poll() is None:
                child.kill()


class ConcurrencyAndDuplicateTests(Env):
    def test_send_is_refused_while_another_job_holds_the_lock(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        holder = RunLock(self.lock)
        self.assertTrue(holder.acquire())
        try:
            with self.assertRaises(RunBusy):
                self.run_send(self.outreach(sheets, gmail), lock_timeout=0.3)
        finally:
            holder.release()
        self.assertEqual((gmail.sent, gmail.attempts, self.store.message_rows()), ([], [], []))

    def test_send_waits_for_a_short_running_refresh_then_proceeds(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        holder = RunLock(self.lock)
        holder.acquire()
        threading.Timer(0.5, holder.release).start()
        self.run_send(self.outreach(sheets, gmail), lock_timeout=10)
        self.assertEqual(len(gmail.sent), 4)

    def test_repeated_runs_never_duplicate(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        o = self.outreach(sheets, gmail)
        for _ in range(3):
            self.run_send(o)
        self.assertEqual(len(gmail.sent), 4)
        self.assertEqual(len(set(self.sent_to(gmail))), 4)

    def test_two_overlapping_senders_sharing_the_database_never_double_send(self):
        sheets = FakeSheetsRW(sheet_data())
        results, gmails = [], [CampaignGmail(delay=0.05), CampaignGmail(delay=0.05)]

        def worker(gmail, i):
            store = TrackingStore(self.store.db_path, clock=Clock())     # its own connection, like another process
            dup = DuplicatePreventionService(os.path.join(self.tmp, f'dup{i}.json'))
            results.append(self.outreach(sheets, gmail, store=store, dup=dup).process_all())
        threads = [threading.Thread(target=worker, args=(g, i)) for i, g in enumerate(gmails)]
        [t.start() for t in threads]
        [t.join(timeout=60) for t in threads]
        everything = [m['to'] for g in gmails for m in g.sent]
        self.assertEqual(sorted(everything), ['cy@c.com', 'fay@f.com', 'fo@x.com', 'rae@r.com'])   # each exactly once

    def test_ambiguous_gmail_outcome_is_never_retried_by_later_runs(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        gmail.errors['fay@f.com'] = RuntimeError('socket timed out')
        o = self.outreach(sheets, gmail)
        self.run_send(o)
        gmail.errors.clear()
        for _ in range(2):
            res = self.run_send(o)
            self.assertEqual(next(r for r in res if r.get('to') == 'fay@f.com')['status'],
                             'SKIP_UNCERTAIN_PREVIOUS_ATTEMPT')
        self.assertEqual(gmail.attempts.count('fay@f.com'), 1)          # attempted exactly once, ever
        self.assertEqual(self.store.get_message_by_key('T-Founders|fay@f.com')['state'], 'uncertain')

    def test_definite_rejection_is_the_only_failure_that_may_be_attempted_again(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        gmail.errors['fay@f.com'] = http_error(400)
        o = self.outreach(sheets, gmail)
        self.run_send(o)
        gmail.errors.clear()
        self.run_send(o)
        self.assertEqual(gmail.attempts.count('fay@f.com'), 2)
        self.assertIn('fay@f.com', self.sent_to(gmail))


# ----------------------------------------------------------------------------------------------------
class CliSendTests(Env):
    """`run.py send` end to end with the unavailable-network pieces mocked."""

    def run_main(self, outreach, locked=False):
        import main
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['run.py', 'send']), \
                mock.patch.object(main, 'DRY_RUN', False), mock.patch.object(main, 'ENABLE_EMAIL_SENDING', True), \
                mock.patch.object(main, 'LOCK_PATH', self.lock), mock.patch.object(main, 'REFRESH_LOG', self.log), \
                mock.patch.object(main, 'build_outreach', return_value=(None, outreach)), \
                contextlib.redirect_stdout(out):
            try:
                main.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_refresh_failure_aborts_the_cli_before_any_email(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail(fail_notices=True)
        code, text = self.run_main(self.outreach(sheets, gmail))
        self.assertEqual(code, 1)
        self.assertIn('NOT SENDING', text)
        self.assertEqual((gmail.sent, gmail.attempts), ([], []))

    def test_busy_lock_aborts_the_cli(self):
        holder = RunLock(self.lock)
        holder.acquire()
        gmail = CampaignGmail()
        try:
            with mock.patch('services.refresh.RunLock.acquire', side_effect=lambda timeout=None: False):
                code, text = self.run_main(self.outreach(FakeSheetsRW(sheet_data()), gmail))
        finally:
            holder.release()
        self.assertEqual(code, 1)
        self.assertIn('NOT SENDING', text)
        self.assertEqual(gmail.attempts, [])

    def test_happy_path_refreshes_then_sends(self):
        gmail = CampaignGmail()
        code, text = self.run_main(self.outreach(FakeSheetsRW(sheet_data()), gmail))
        self.assertEqual(code, 0)
        self.assertEqual(len(gmail.sent), 4)
        self.assertIn('SENT', text)

    def test_the_safety_locks_still_stop_a_real_cli_run_before_any_network_use(self):
        env = {**os.environ, 'DRY_RUN': 'true', 'ENABLE_EMAIL_SENDING': 'false'}
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'run.py'), 'send'], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0)
        self.assertIn('Sending disabled in config', r.stdout)


# ----------------------------------------------------------------------------------------------------
class ScheduledJobTests(Env):
    def build_for(self, sheets, gmail, store=None):
        store = store or self.store
        writer = SheetStatusWriter(sheets, MAPPING, 'SID', sleep=lambda s: None)
        return lambda: (store, gmail, writer, SETTINGS)

    def job(self, build, **kw):
        return run_refresh_job(build, self.lock, self.log, **kw)

    def log_text(self):
        with open(self.log, encoding='utf-8') as f:
            return f.read()

    class NoSendGmail(CampaignGmail):
        def send_email(self, *a, **k):
            raise AssertionError('the scheduled refresh must never send email')

    def test_success_records_counts_and_changes_the_sheet_only_via_status_sync(self):
        send(self.store, key='T-Recruit|fo@x.com', tab='T-Recruit', row=3, to='fo@x.com', gid='gFo', tid='thrFo')
        sheets = FakeSheetsRW(data_with_earlier_send())
        gmail = self.NoSendGmail(threads={'thrFo': [msg('r1', LATER, frm='Fo <fo@x.com>')]})
        code, info = self.job(self.build_for(sheets, gmail))
        self.assertEqual((code, info['status'], info['sheet_writes']), (0, 'OK', 1))
        self.assertEqual(sheets.data['T-Recruit'][2][0], 'RESPONDED')
        text = self.log_text()
        self.assertRegex(text, r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d')       # timestamped
        self.assertIn('status=OK', text)
        self.assertIn('reply=1', text)
        self.assertIn('sheet_writes=1', text)
        self.assertEqual((gmail.sent, gmail.attempts), ([], []))

    def test_it_never_sends_even_with_pending_rows(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), self.NoSendGmail()
        code, _ = self.job(self.build_for(sheets, gmail))
        self.assertEqual(code, 0)
        self.assertEqual([u for u in sheets.updates if u[1] == 'EMAIL_SENT'], [])   # pending rows untouched
        self.assertEqual(sheets.data, sheet_data())                                  # nothing in the sheet changed

    def test_no_sending_code_path_exists_in_the_job(self):
        src = open(os.path.join(ROOT, 'services', 'refresh.py'), encoding='utf-8').read()
        tree = ast.parse(src)
        job = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'run_refresh_job')
        names = {n.id for n in ast.walk(job) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(job) if isinstance(n, ast.Attribute)}
        self.assertEqual(names & {'send_email', 'process_all', 'OutreachService', 'send_campaign', 'build_outreach'}, set())
        main_src = open(os.path.join(ROOT, 'main.py'), encoding='utf-8').read()
        block = main_src.split("elif args.mode == 'refresh':")[1].split("elif args.mode ==")[0]
        for forbidden in ('send_campaign', 'OutreachService', 'process_all', 'build_outreach', 'send_email'):
            self.assertNotIn(forbidden, block)
        self.assertIn('interactive=False', block)

    def test_failure_exits_nonzero_and_logs_only_a_sanitized_error(self):
        secret = 'ya29.A0ARrdaM-' + 'x' * 40
        class Leaky(self.NoSendGmail):
            def find_bounces(self, *a, **k):
                raise RuntimeError(f'denied for someone@private.org token {secret}')
        code, info = self.job(self.build_for(FakeSheetsRW(sheet_data()), Leaky()))
        self.assertEqual((code, info['status']), (1, 'FAILED'))
        text = self.log_text()
        self.assertIn('status=FAILED', text)
        self.assertIn('RefreshError', text)
        self.assertNotIn('someone@private.org', text)
        self.assertNotIn(secret, text)

    def test_auth_that_needs_a_browser_fails_instead_of_hanging(self):
        def build():
            raise AuthRequired('expired')
        code, info = self.job(build)
        self.assertEqual((code, info['error']), (1, 'AuthRequired'))
        self.assertIn('AuthRequired', self.log_text())

    def test_overlapping_run_is_skipped_without_touching_anything(self):
        holder = RunLock(self.lock)
        self.assertTrue(holder.acquire())
        called = []
        try:
            code, info = self.job(lambda: called.append(1))
        finally:
            holder.release()
        self.assertEqual((code, info['status'], called), (0, 'SKIPPED_LOCKED', []))
        self.assertIn('status=SKIPPED_LOCKED', self.log_text())
        # and the next run, after the holder finished, works
        code, info = self.job(self.build_for(FakeSheetsRW(sheet_data()), self.NoSendGmail()))
        self.assertEqual((code, info['status']), (0, 'OK'))

    def test_logs_contain_no_addresses_or_message_text_even_when_events_are_found(self):
        send(self.store, key='T-Recruit|fo@x.com', tab='T-Recruit', row=3, to='fo@x.com', gid='gFo', tid='thrFo')
        sheets = FakeSheetsRW(data_with_earlier_send())
        gmail = self.NoSendGmail(threads={'thrFo': [msg('r1', LATER, frm='Fo <fo@x.com>', snippet='SECRET BODY TEXT')]})
        self.job(self.build_for(sheets, gmail))
        text = self.log_text()
        self.assertNotIn('fo@x.com', text)
        self.assertNotIn('SECRET', text)

    def test_unattended_gmail_login_never_opens_a_browser(self):
        d = tempfile.mkdtemp()
        token = os.path.join(d, 'token.json')
        with open(token, 'w') as f:
            json.dump({'scopes': SCOPES}, f)             # token lacks gmail.readonly
        with mock.patch('services.gmail_service.Credentials') as C, \
                mock.patch('services.gmail_service.InstalledAppFlow') as F, \
                mock.patch('services.gmail_service.build'):
            C.from_authorized_user_file.return_value = mock.MagicMock(valid=True)
            with self.assertRaises(AuthRequired):
                from services.gmail_service import SCOPES_WITH_READ
                GmailService('credentials.json', token, scopes=SCOPES_WITH_READ, interactive=False)
            F.from_client_secrets_file.assert_not_called()


class SanitizeTests(unittest.TestCase):
    def test_masks_addresses_tokens_and_long_text(self):
        out = sanitize('fail for a.b@c.com with ya29.' + 'A' * 50 + ' ok')
        self.assertNotIn('a.b@c.com', out)
        self.assertNotIn('AAAAAAAA', out)
        self.assertLessEqual(len(sanitize('word ' * 500)), 160)


class TaskSchedulerSetupTests(unittest.TestCase):
    SCRIPT = os.path.join(ROOT, 'scripts', 'install_refresh_task.ps1')

    @classmethod
    def setUpClass(cls):
        with open(cls.SCRIPT, encoding='utf-8') as f:
            cls.text = f.read()

    def test_task_runs_refresh_only_every_15_minutes_without_overlap(self):
        code = re.sub(r'<#.*?#>', '', self.text, flags=re.S)          # ignore the help text...
        code = re.sub(r'(?m)^\s*#.*$', '', code)                       # ...and comments: check what actually runs
        self.assertRegex(code, r"\$arguments\s*=\s*'run\.py refresh'")
        self.assertNotRegex(code, r"run\.py\s+send")
        self.assertEqual(re.findall(r"-Argument\s+(\S+)", code), ['$arguments'])   # the only argument the task passes
        self.assertRegex(self.text, r'\[int\]\$IntervalMinutes\s*=\s*15')
        self.assertIn('-MultipleInstances IgnoreNew', self.text)
        self.assertIn('-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)', self.text)
        self.assertIn('-ExecutionTimeLimit (New-TimeSpan -Minutes 10)', self.text)

    def test_registration_requires_an_explicit_install_switch(self):
        before_register = self.text.split('Register-ScheduledTask -TaskName')[0]
        self.assertIn('if (-not $Install)', before_register)
        self.assertEqual(self.text.count('Register-ScheduledTask -TaskName'), 1)

    def test_no_credentials_or_password_in_the_script(self):
        self.assertNotRegex(self.text, r'(?i)-Password|client_secret|refresh_token|\.env|token\.json')
        self.assertIn('no stored password', self.text)

    @unittest.skipUnless(shutil.which('powershell'), 'PowerShell not available')
    def test_plan_mode_runs_and_registers_nothing(self):
        r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', self.SCRIPT,
                            '-TaskName', 'pyEmail-Refresh-UnitTest'], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('run.py refresh', r.stdout)
        self.assertIn('Nothing was registered', r.stdout)
        chk = subprocess.run(['powershell', '-NoProfile', '-Command',
                              "(Get-ScheduledTask -TaskName 'pyEmail-Refresh-UnitTest' -ErrorAction SilentlyContinue | "
                              "Measure-Object).Count"], capture_output=True, text=True, timeout=60)
        self.assertEqual(chk.stdout.strip(), '0')

    def test_documentation_exists(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, 'docs', 'refresh-task.md')))


if __name__ == '__main__':
    unittest.main()
