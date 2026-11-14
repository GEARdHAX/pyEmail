"""Mocked tests for the bounce checker. Tab names/addresses are synthetic."""
import unittest

from services.bounce_service import BounceService, failed_addresses
from tests.test_outreach import FakeSheets

CFG = {'header_row': 1, 'merge_status_header': 'Merge status', 'sent_value': 'EMAIL_SENT',
       'tabs': {'A': {'template': 'cold', 'email_header': 'Email'},
                'B': {'template': 'cold', 'email_header': 'Email'}}}


class FakeBounceGmail:
    def __init__(self, notices):
        self.notices = notices

    def find_bounces(self, days=45, max_messages=200):
        return self.notices


def notice(subject, failed='', snippet=''):
    return {'id': 'n', 'internalDate': 1, 'subject': subject, 'failed': failed, 'snippet': snippet}


DSN = 'Delivery Status Notification (Failure)'


class FailedAddressTests(unittest.TestCase):
    known = {'bad@x.com', 'ok@x.com'}

    def test_header_and_snippet(self):
        self.assertEqual(failed_addresses(notice(DSN, 'Bad@X.com'), self.known), {'bad@x.com'})
        self.assertEqual(failed_addresses(notice(DSN, '', 'Your message to bad@x.com was not delivered'),
                                          self.known), {'bad@x.com'})

    def test_delay_and_unrelated_ignored(self):
        self.assertEqual(failed_addresses(notice('Delivery Status Notification (Delay)', 'bad@x.com'),
                                          self.known), set())
        self.assertEqual(failed_addresses(notice('Weekly digest', 'bad@x.com'), self.known), set())

    def test_unknown_addresses_ignored(self):
        self.assertEqual(failed_addresses(notice(DSN, 'stranger@y.com'), self.known), set())


class BounceServiceTests(unittest.TestCase):
    def data(self):
        return {
            'A': [['Email', 'Merge status', 'Campaign status'],
                  ['bad@x.com', 'EMAIL_SENT', 'keep'],
                  ['ok@x.com', 'EMAIL_SENT', ''],
                  ['bad@x.com', 'BOUNCED', '']],
            'B': [['Merge status', 'Email'],
                  ['', 'bad@x.com'],               # same address, never sent: blank -> BOUNCED
                  ['EMAIL_OPENED', 'bad@x.com'],   # contradictory status: left alone
                  ['RESPONDED', 'bad@x.com']],     # left alone
        }

    def run_it(self, apply):
        data = self.data()
        sheets = FakeSheets(data)

        def get(sid, rng):
            tab = rng.split('!')[0].strip("'")
            col = ord(rng.split('!')[1][0]) - 65
            row = int(rng.split('!')[1][1:])
            return [[data[tab][row - 1][col]]]
        sheets.get_sheet_data = get
        svc = BounceService(sheets, FakeBounceGmail([notice(DSN, 'bad@x.com')]), CFG, 'SID')
        return svc.check_all(apply=apply), sheets

    def test_preview_writes_nothing(self):
        res, sheets = self.run_it(False)
        self.assertEqual(sheets.updates, [])
        self.assertEqual({(r['tab'], r['row']): r['result'] for r in res},
                         {('A', 2): 'WOULD_MARK', ('A', 4): 'ALREADY_BOUNCED', ('B', 2): 'WOULD_MARK',
                          ('B', 3): 'LEFT_ALONE_OTHER_STATUS', ('B', 4): 'LEFT_ALONE_OTHER_STATUS'})

    def test_apply_marks_across_tabs_without_touching_other_columns_or_statuses(self):
        res, sheets = self.run_it(True)
        self.assertEqual(sorted(sheets.updates), [("'A'!B2", 'BOUNCED'), ("'B'!A2", 'BOUNCED')])
        self.assertNotIn('ok@x.com', [r['to'] for r in res])  # healthy address untouched

    def test_status_changed_since_read_is_not_overwritten(self):
        data = self.data()
        sheets = FakeSheets(data)
        sheets.get_sheet_data = lambda sid, rng: [['EMAIL_OPENED']]
        svc = BounceService(sheets, FakeBounceGmail([notice(DSN, 'bad@x.com')]), CFG, 'SID')
        res = svc.check_all(apply=True)
        self.assertEqual(sheets.updates, [])
        self.assertIn('NOT_MARKED_STATUS_CHANGED', [r['result'] for r in res])


if __name__ == '__main__':
    unittest.main()
