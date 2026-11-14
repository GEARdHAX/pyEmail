"""Mocked tests for the tracked test-email command. No real email is sent."""
import unittest

from services.test_email import BODY, SUBJECT, send_test_email
from services.tracking_settings import TrackingSettings
from tests.test_tracking_core import Clock, make_store
from tests.test_tracking_integration import FakeGmail2, http_error

ON = TrackingSettings(enabled=True, base_url='https://t.example.com')


class TestEmailTests(unittest.TestCase):
    def setUp(self):
        self.store, self.gmail = make_store(Clock()), FakeGmail2()

    def test_refuses_when_tracking_is_not_enabled(self):
        for s in (TrackingSettings(), TrackingSettings(enabled=True)):
            with self.assertRaises(ValueError):
                send_test_email(self.store, self.gmail, s, 'me@x.com', apply=True)
        self.assertEqual(self.gmail.sent, [])

    def test_rejects_bad_addresses(self):
        with self.assertRaises(ValueError):
            send_test_email(self.store, self.gmail, ON, 'not-an-address', apply=True)

    def test_preview_sends_and_records_nothing(self):
        r = send_test_email(self.store, None, ON, 'me@x.com')
        self.assertEqual((r['status'], r['sent']), ('PREVIEW', False))
        self.assertEqual(self.store.message_rows(), [])

    def test_apply_sends_one_tracked_message_and_records_it(self):
        r = send_test_email(self.store, self.gmail, ON, 'me@x.com', apply=True)
        self.assertEqual((r['status'], len(self.gmail.sent)), ('SENT', 1))
        m = self.gmail.sent[0]
        self.assertEqual((m['to'], m['subject'], m['body'], m['subtype']), ('me@x.com', SUBJECT, BODY, 'plain'))
        row = self.store.message_rows(state='sent')[0]
        self.assertEqual((row['tab'], row['campaign'], row['tracking_enabled'], row['link_count']),
                         ('TEST', 'tracking-test', 1, 1))
        self.assertIn(f"https://t.example.com/o/{row['tracking_id']}.gif", m['html_body'])
        self.assertIn('https://www.linkedin.com/in/adarsharya2911/', m['body'])  # plain text keeps the real URL

    def test_repeatable_without_colliding(self):
        send_test_email(self.store, self.gmail, ON, 'me@x.com', apply=True)
        send_test_email(self.store, self.gmail, ON, 'me@x.com', apply=True)
        self.assertEqual(len(self.store.message_rows(state='sent')), 2)

    def test_failures_follow_the_same_uncertain_rules(self):
        self.gmail.errors['me@x.com'] = http_error(400)
        self.assertEqual(send_test_email(self.store, self.gmail, ON, 'me@x.com', apply=True)['status'], 'SEND_FAILED')
        self.assertEqual(self.store.uncertain_messages(), [])          # definite rejection: nothing left behind
        self.gmail.errors['me@x.com'] = RuntimeError('timeout')
        send_test_email(self.store, self.gmail, ON, 'me@x.com', apply=True)
        self.assertEqual(len(self.store.uncertain_messages()), 1)       # ambiguous: parked, never retried


if __name__ == '__main__':
    unittest.main()
