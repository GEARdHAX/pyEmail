"""`daily --send`: the person chooses how many to send by typing  SEND <number>  (mocked; nothing real is sent)."""
from tests.test_daily import DailyBase
from tests.test_outreach import sheet_data
from tests.test_refresh_and_send import CampaignGmail
from tests.test_tracking_integration import FakeSheetsRW


class ChooseAmountTests(DailyBase):
    def go(self, answer, sheets=None, gmail=None, **kw):
        sheets = sheets or FakeSheetsRW(sheet_data())
        gmail = gmail or CampaignGmail()
        result, _ = self.daily(sheets, gmail, send=True, locks_open=True, ask=self.ask_with(answer), **kw)
        return result, sheets, gmail

    def statuses(self, sheets):
        """Merge status of each tab's first data row (the four ready recipients)."""
        col = {'T-Founders': 0, 'T-Cold': 8, 'T-Recruit': 0, 'T-Follow': 6}
        out = {}
        for tab, c in col.items():
            row = sheets.data[tab][1] + [''] * 12
            out[tab] = row[c]
        return out

    def test_prompt_tells_you_the_range(self):
        self.go('x')
        self.assertIn('SEND <number>  (1 to 4)', self.prompts[0])

    def test_you_can_send_fewer_than_are_ready(self):
        result, sheets, gmail = self.go('SEND 2')
        self.assertEqual((result['status'], result['sent']), ('SENT', 2))
        self.assertEqual(len(gmail.attempts), 2)
        self.assertIn('sending 2 of the 4 ready', self.text)
        done = self.statuses(sheets)
        self.assertEqual([done['T-Founders'], done['T-Cold']], ['EMAIL_SENT', 'EMAIL_SENT'])   # first two, in tab order
        self.assertEqual([done['T-Recruit'], done['T-Follow']], ['', ''])                      # the rest stay pending

    def test_you_can_send_just_one(self):
        result, _, gmail = self.go('SEND 1')
        self.assertEqual((result['sent'], gmail.attempts), (1, ['fay@f.com']))

    def test_the_rest_stays_available_for_the_next_run(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        self.go('SEND 1', sheets, gmail)
        self.lines.clear()
        result, _, _ = self.go('SEND 3', sheets, gmail)
        self.assertIn('3 email(s) are ready', self.text)
        self.assertEqual(result['sent'], 3)
        self.assertEqual(sorted(gmail.attempts), ['cy@c.com', 'fay@f.com', 'fo@x.com', 'rae@r.com'])   # each exactly once

    def test_the_maximum_still_works(self):
        result, _, gmail = self.go('SEND 4')
        self.assertEqual((result['sent'], len(gmail.attempts)), (4, 4))

    def test_lowercase_and_extra_spaces_are_fine(self):
        for answer in ('send 2', '  SEND   2  ', 'Send 2'):
            self.setUp()                                  # fresh send record each time
            result, _, gmail = self.go(answer, FakeSheetsRW(sheet_data()), CampaignGmail())
            self.assertEqual((answer, result['sent']), (answer, 2))

    def test_more_than_are_ready_cancels(self):
        result, _, gmail = self.go('SEND 9')
        self.assertEqual((result['status'], gmail.attempts), ('CANCELLED', []))
        self.assertIn('9 is not between 1 and 4', self.text)

    def test_more_than_the_daily_cap_cancels(self):
        result, _, gmail = self.go('SEND 3', limit=2)
        self.assertEqual((result['status'], gmail.attempts), ('CANCELLED', []))
        self.assertIn('3 is not between 1 and 2', self.text)

    def test_zero_and_nonsense_cancel_and_send_nothing(self):
        for answer in ('SEND 0', 'SEND', 'SEND two', 'SEND -1', 'SEND 2 3', 'SEND 2.5', 'yes', '', '2', 'SENDALL'):
            self.setUp()
            result, _, gmail = self.go(answer, FakeSheetsRW(sheet_data()), CampaignGmail())
            self.assertEqual((answer, result['status'], gmail.attempts), (answer, 'CANCELLED', []))

    def test_cap_and_choice_work_together(self):
        result, _, gmail = self.go('SEND 1', limit=2)
        self.assertEqual((result['sent'], len(gmail.attempts)), (1, 1))
        self.assertIn('the cap is 2, so 2 will wait for the next run', self.text)

    def test_yes_flag_still_sends_the_maximum_without_asking(self):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        result, _ = self.daily(sheets, gmail, send=True, locks_open=True, assume_yes=True, limit=3)
        self.assertEqual((result['sent'], self.prompts), (3, []))

    def test_final_safety_checks_still_apply_to_the_chosen_amount(self):
        sheets = FakeSheetsRW(sheet_data())
        sheets.fresh_override[('T-Founders', 2)] = ['RESPONDED', 'Fay', 'fay@f.com', 'FCo', 'Engineer', 'Delhi', 'a', 'b']
        result, _, gmail = self.go('SEND 2', sheets)
        self.assertNotIn('fay@f.com', gmail.attempts)        # skipped at the last-moment check, not sent
        self.assertEqual(result['sent'], 2)                   # but two other recipients still go out


if __name__ == '__main__':
    import unittest
    unittest.main()
