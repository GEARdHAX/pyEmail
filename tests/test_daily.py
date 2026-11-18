"""Mocked tests for the daily master command (`run.py daily`). Nothing real is sent."""
import contextlib
import io
import os
import sys
import tempfile
from unittest import mock

from services.daily import DEFAULT_LIMIT, find_duplicates, run_daily, status_table
from services.run_lock import RunLock
from tests.test_ingest_classification import LATER, msg
from tests.test_outreach import MAPPING, sheet_data
from tests.test_refresh_and_send import CampaignGmail, Env, data_with_earlier_send
from tests.test_tracking_core import T0, send
from tests.test_tracking_integration import FakeSheetsRW, http_error


class DailyBase(Env):
    def setUp(self):
        super().setUp()
        self.daily_log = os.path.join(self.tmp, 'daily.log')
        self.lines, self.prompts = [], []

    def say(self, text=''):
        self.lines.append(str(text))

    def ask_with(self, answer):
        def ask(prompt):
            self.prompts.append(prompt)
            return answer
        return ask

    def daily(self, sheets, gmail, **kw):
        o = self.outreach(sheets, gmail)
        kw.setdefault('ask', self.ask_with('nope'))
        kw.setdefault('lock_timeout', 0.2)
        result = run_daily(o, MAPPING, self.lock, self.daily_log, say=self.say, **kw)
        return result, o

    @property
    def text(self):
        return '\n'.join(self.lines)


class ReportTests(DailyBase):
    def test_preview_run_shows_all_six_sections_and_sends_nothing(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        result, _ = self.daily(sheets, gmail)
        self.assertEqual(result['status'], 'PREVIEW')
        for section in ('1/6  SAFETY LOCKS', '2/6  REFRESH', '3/6  STATUS DASHBOARD', '4/6  PLAN',
                        '5/6  NEEDS YOUR ATTENTION', '6/6  SEND'):
            self.assertIn(section, self.text)
        self.assertIn('LOCKED (preview only)', self.text)
        self.assertIn('ready to send: 4', self.text)
        self.assertIn('python run.py daily --send', self.text)
        self.assertEqual((gmail.sent, gmail.attempts, self.prompts), ([], [], []))   # never asks, never sends

    def test_new_bounces_and_replies_are_listed_once(self):
        sheets, gmail = self.earlier_send([msg('r1', LATER, frm='Fo <fo@x.com>')])
        self.daily(sheets, gmail)
        self.assertIn('NEW since last run', self.text)
        self.assertRegex(self.text, r'reply\s+\[T-Recruit\] fo@x\.com')
        self.lines.clear()
        self.daily(sheets, gmail)
        self.assertIn('Nothing new since the last run', self.text)

    def test_preview_with_nothing_pending_says_so_plainly(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        self.daily(sheets, gmail, send=True, locks_open=True, assume_yes=True)     # send everything first
        self.lines.clear()
        result, _ = self.daily(sheets, gmail)
        self.assertEqual(result['status'], 'PREVIEW')
        self.assertIn('No emails are ready', self.text)
        self.assertNotIn('0 ready email', self.text)

    def test_dashboard_counts_each_status_per_tab(self):
        data = sheet_data()
        data['T-Founders'][3][0] = 'EMAIL_OPENED'
        table = status_table(FakeSheetsRW(data), 'SID', MAPPING)
        self.assertEqual(table['T-Founders']['PENDING'], 2)       # fay + the row missing a Job_Role
        self.assertEqual(table['T-Founders']['EMAIL_OPENED'], 1)
        self.assertNotIn('', table['T-Founders'])                 # invalid-email rows are not counted

    def test_attention_list_names_the_row_and_the_fix(self):
        data = sheet_data()
        data['T-Cold'][1].append('MYSTERY')                        # unknown status value
        sheets, gmail = FakeSheetsRW(data), CampaignGmail()
        self.daily(sheets, gmail)
        self.assertIn('SKIP_MISSING_FIELDS', self.text)
        self.assertIn('T-Founders row 6', self.text)
        self.assertIn('SKIP_INVALID_EMAIL', self.text)
        self.assertIn('T-Founders row 5', self.text)
        self.assertIn('SKIP_UNKNOWN_STATUS', self.text)
        self.assertIn('T-Cold row 2', self.text)

    def test_duplicate_addresses_within_a_tab_are_reported(self):
        data = sheet_data()
        data['T-Recruit'].append(['', 'Rae again', 'RAE@r.com', 'RCo', 'Dev', '5', 'Py', 'SQL', 'Goa', 'P', 'ach'])
        dups = find_duplicates(FakeSheetsRW(data), 'SID', MAPPING)
        self.assertEqual(dups, {'T-Recruit': [('rae@r.com', [2, 3])]})
        self.daily(FakeSheetsRW(data), CampaignGmail())
        self.assertIn('DUPLICATE ADDRESS in T-Recruit: rows 2, 3', self.text)
        self.assertIn('ready to send: 4', self.text)                       # 5 rows, but only 4 distinct people
        self.assertIn('1 duplicate-address row(s) will not be emailed twice', self.text)

    def test_uncertain_sends_are_called_out(self):
        self.store.reserve_send('T-Founders|zed@f.com', 'T-Founders', 9, 'zed@f.com', 'c', 's', False)
        self.store.mark_uncertain(self.store.get_message_by_key('T-Founders|zed@f.com')['id'])
        self.daily(FakeSheetsRW(sheet_data()), CampaignGmail())
        self.assertIn('UNCERTAIN SEND', self.text)
        self.assertIn('track uncertain', self.text)

    def test_all_clear_message_when_nothing_needs_attention(self):
        data = {'T-Cold': [r for r in sheet_data()['T-Cold']]}
        cfg_tabs = {'T-Cold': MAPPING['tabs']['T-Cold']}
        o = self.outreach(FakeSheetsRW(data), CampaignGmail())
        o.cfg = {**MAPPING, 'tabs': cfg_tabs}
        o.writer.cfg = o.cfg
        run_daily(o, o.cfg, self.lock, self.daily_log, say=self.say)
        self.assertIn('Nothing. All clear.', self.text)

    def test_log_holds_counts_only(self):
        sheets, gmail = self.earlier_send([msg('r1', LATER, frm='Fo <fo@x.com>', snippet='PRIVATE BODY')])
        self.daily(sheets, gmail)
        log = open(self.daily_log, encoding='utf-8').read()
        self.assertIn('daily status=PREVIEW', log)
        self.assertIn('new_events=1', log)
        for private in ('fo@x.com', 'PRIVATE', 'f.com'):
            self.assertNotIn(private, log)


class SendGatingTests(DailyBase):
    def test_locked_config_never_sends_and_shows_the_one_time_unlock(self):
        gmail = CampaignGmail()
        result, _ = self.daily(FakeSheetsRW(sheet_data()), gmail, send=True, locks_open=False, assume_yes=True)
        self.assertEqual(result['status'], 'LOCKED')
        self.assertIn("$env:DRY_RUN='false'", self.text)
        self.assertEqual((gmail.sent, gmail.attempts, self.prompts), ([], [], []))

    def test_wrong_confirmation_cancels(self):
        gmail = CampaignGmail()
        result, _ = self.daily(FakeSheetsRW(sheet_data()), gmail, send=True, locks_open=True,
                               ask=self.ask_with('yes'))
        self.assertEqual(result['status'], 'CANCELLED')
        self.assertIn('SEND 4', self.prompts[0])
        self.assertEqual(gmail.attempts, [])

    def test_exact_confirmation_refreshes_then_sends(self):
        sheets, gmail = self.earlier_send([msg('r1', LATER, frm='Fo <fo@x.com>')])
        result, _ = self.daily(sheets, gmail, send=True, locks_open=True, ask=self.ask_with('SEND 3'))
        self.assertEqual((result['status'], result['sent']), ('SENT', 3))
        self.assertEqual(self.sent_to(gmail), ['cy@c.com', 'fay@f.com', 'rae@r.com'])   # replied fo@x.com is excluded
        self.assertIn('DONE. sent=3', self.text)

    def test_yes_flag_skips_the_prompt(self):
        gmail = CampaignGmail()
        result, _ = self.daily(FakeSheetsRW(sheet_data()), gmail, send=True, locks_open=True, assume_yes=True)
        self.assertEqual((result['status'], len(gmail.sent), self.prompts), ('SENT', 4, []))

    def test_non_interactive_without_yes_sends_nothing(self):
        gmail = CampaignGmail()
        result, _ = self.daily(FakeSheetsRW(sheet_data()), gmail, send=True, locks_open=True, interactive=False)
        self.assertEqual((result['status'], gmail.attempts), ('NEEDS_CONFIRMATION', []))

    def test_nothing_to_send_is_reported_without_a_prompt(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        self.daily(sheets, gmail, send=True, locks_open=True, assume_yes=True)
        self.lines.clear()
        result, _ = self.daily(sheets, gmail, send=True, locks_open=True, ask=self.ask_with('x'))
        self.assertEqual(result['status'], 'NOTHING_TO_SEND')
        self.assertEqual(len(gmail.sent), 4)

    def test_daily_cap_limits_attempts_and_the_rest_waits_for_the_next_run(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        result, _ = self.daily(sheets, gmail, send=True, locks_open=True, limit=2, ask=self.ask_with('SEND 2'))
        self.assertEqual((result['sent'], result['held_back'], len(gmail.attempts)), (2, 2, 2))
        self.assertIn('2 will wait for the next run', self.text)
        result, _ = self.daily(sheets, gmail, send=True, locks_open=True, limit=2, ask=self.ask_with('SEND 2'))
        self.assertEqual(result['sent'], 2)
        self.assertEqual(sorted(gmail.attempts), ['cy@c.com', 'fay@f.com', 'fo@x.com', 'rae@r.com'])   # each exactly once

    def test_default_cap_is_100(self):
        self.assertEqual(DEFAULT_LIMIT, 100)

    def test_final_check_still_applies_inside_the_daily_send(self):
        sheets = FakeSheetsRW(sheet_data())
        sheets.fresh_override[('T-Founders', 2)] = ['RESPONDED', 'Fay', 'fay@f.com', 'FCo', 'Engineer', 'Delhi', 'a', 'b']
        gmail = CampaignGmail()
        self.daily(sheets, gmail, send=True, locks_open=True, assume_yes=True)
        self.assertNotIn('fay@f.com', self.sent_to(gmail))


class FailClosedTests(DailyBase):
    def test_refresh_failure_stops_before_any_send_or_prompt(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail(fail_notices=True)
        result, _ = self.daily(sheets, gmail, send=True, locks_open=True, assume_yes=True)
        self.assertEqual(result['status'], 'REFRESH_FAILED')
        self.assertIn('Nothing was sent', self.text)
        self.assertNotIn('4/6  PLAN', self.text)
        self.assertEqual((gmail.attempts, self.prompts), ([], []))

    def test_busy_lock_stops_everything(self):
        holder = RunLock(self.lock)
        self.assertTrue(holder.acquire())
        try:
            gmail = CampaignGmail()
            result, _ = self.daily(FakeSheetsRW(sheet_data()), gmail, send=True, locks_open=True, assume_yes=True)
        finally:
            holder.release()
        self.assertEqual((result['status'], gmail.attempts), ('BUSY', []))
        self.assertTrue(RunLock(self.lock).acquire())      # and it left nothing locked behind

    def test_lock_is_released_after_a_normal_run(self):
        self.daily(FakeSheetsRW(sheet_data()), CampaignGmail())
        probe = RunLock(self.lock)
        self.assertTrue(probe.acquire())
        probe.release()


class CliWiringTests(DailyBase):
    def run_cli(self, argv, outreach, locks=(True, False)):
        import main
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['run.py', *argv]), \
                mock.patch.object(main, 'DRY_RUN', locks[0]), mock.patch.object(main, 'ENABLE_EMAIL_SENDING', locks[1]), \
                mock.patch.object(main, 'LOCK_PATH', self.lock), mock.patch.object(main, 'DAILY_LOG', self.daily_log), \
                mock.patch.object(main, 'REFRESH_LOG', self.log), \
                mock.patch.object(main, 'build_outreach', return_value=(None, outreach)), \
                contextlib.redirect_stdout(out):
            try:
                main.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_daily_with_locks_on_is_a_safe_report(self):
        gmail = CampaignGmail()
        code, text = self.run_cli(['daily'], self.outreach(FakeSheetsRW(sheet_data()), gmail))
        self.assertEqual(code, 0)
        self.assertEqual(gmail.attempts, [])

    def test_daily_send_with_locks_on_does_not_send(self):
        gmail = CampaignGmail()
        code, _ = self.run_cli(['daily', '--send', '--yes'], self.outreach(FakeSheetsRW(sheet_data()), gmail),
                               locks=(True, False))
        self.assertEqual((code, gmail.attempts), (0, []))

    def test_daily_exit_code_signals_failure(self):
        code, _ = self.run_cli(['daily'], self.outreach(FakeSheetsRW(sheet_data()), CampaignGmail(fail_notices=True)))
        self.assertEqual(code, 1)

    def test_limit_flag_reaches_the_plain_send_command(self):
        gmail = CampaignGmail()
        o = self.outreach(FakeSheetsRW(sheet_data()), gmail)
        self.run_cli(['send', '--limit', '1'], o, locks=(False, True))
        self.assertEqual(len(gmail.attempts), 1)
        self.assertEqual(o.send_limit, 1)


if __name__ == '__main__':
    import unittest
    unittest.main()
