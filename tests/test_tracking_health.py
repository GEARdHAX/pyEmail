"""The tracking-address guard: with click tracking on, never send emails whose links would be dead."""
import socket
import threading

from services.refresh import send_campaign
from services.run_lock import RunLock
from services.tracking_health import TrackingUnavailable, check_tracking_endpoint, guard_before_send
from services.tracking_server import make_server
from services.tracking_settings import TrackingSettings
from tests.test_daily import DailyBase
from tests.test_outreach import sheet_data
from tests.test_refresh_and_send import CampaignGmail, Env
from tests.test_tracking_core import Clock, make_store
from tests.test_tracking_integration import FakeSheetsRW

URL = 'https://t.example.com'
CLICKS = TrackingSettings(enabled=True, base_url=URL, track_clicks=True)
OPENS_ONLY = TrackingSettings(enabled=True, base_url=URL, track_clicks=False)
OFF = TrackingSettings()
DOWN = lambda s: (False, 'ConnectError')
UP = lambda s: (True, 'HTTP 200')


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProbeTests(Env):
    def test_tracking_off_needs_nothing(self):
        self.assertEqual(check_tracking_endpoint(OFF), (True, 'tracking off'))

    def test_a_real_running_server_is_reachable(self):
        server = make_server(make_store(Clock()), TrackingSettings(rate_limit_per_min=1000), host='127.0.0.1', port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            s = TrackingSettings(enabled=True, base_url=f'http://127.0.0.1:{server.server_address[1]}')
            self.assertEqual(check_tracking_endpoint(s), (True, 'HTTP 200'))
        finally:
            server.shutdown()
            server.server_close()

    def test_nothing_listening_is_reported_without_raising(self):
        s = TrackingSettings(enabled=True, base_url=f'http://127.0.0.1:{free_port()}')
        ok, detail = check_tracking_endpoint(s, timeout=3)
        self.assertFalse(ok)
        self.assertNotIn('127.0.0.1', detail)       # only an error class name: nothing sensitive

    def test_a_wrong_answer_is_not_healthy(self):
        # a different service on the port (not our /healthz "ok") must not count as the tracker
        import http.server

        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'<html>other</html>')

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Other)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            s = TrackingSettings(enabled=True, base_url=f'http://127.0.0.1:{srv.server_address[1]}')
            self.assertFalse(check_tracking_endpoint(s)[0])
        finally:
            srv.shutdown()
            srv.server_close()


class GuardTests(Env):
    def test_clicks_on_and_address_down_raises(self):
        with self.assertRaisesRegex(TrackingUnavailable, 'TRACKING_CLICKS=false'):
            guard_before_send(CLICKS, DOWN)

    def test_opens_only_and_address_down_only_warns(self):
        self.assertEqual(guard_before_send(OPENS_ONLY, DOWN), (False, 'ConnectError'))

    def test_healthy_or_off_passes(self):
        self.assertTrue(guard_before_send(CLICKS, UP)[0])
        called = []
        guard_before_send(OFF, lambda s: called.append(1) or (False, 'x'))
        self.assertEqual(called, [])               # tracking off: the address is never even probed


class SendCampaignGuardTests(Env):
    def start(self, track, probe):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        o = self.outreach(sheets, gmail)
        o.track, o.tracking_probe = track, probe
        return o, gmail

    def test_click_tracking_with_dead_address_sends_nothing_and_reserves_nothing(self):
        o, gmail = self.start(CLICKS, DOWN)
        with self.assertRaises(TrackingUnavailable):
            self.run_send(o)
        self.assertEqual((gmail.attempts, self.store.message_rows(), o.sheets.updates), ([], [], []))
        probe = RunLock(self.lock)
        self.assertTrue(probe.acquire())           # and the lock was released
        probe.release()

    def test_opens_only_with_dead_address_still_sends_but_is_logged(self):
        o, gmail = self.start(OPENS_ONLY, DOWN)
        self.run_send(o)
        self.assertEqual(len(gmail.sent), 4)
        self.assertIn('tracking_address=UNREACHABLE', open(self.log, encoding='utf-8').read())

    def test_healthy_address_sends_tracked_emails(self):
        o, gmail = self.start(CLICKS, UP)
        self.run_send(o)
        self.assertEqual(len(gmail.sent), 4)
        self.assertTrue(all(m['html_body'] for m in gmail.sent))

    def test_tracking_off_never_probes(self):
        calls = []
        o, gmail = self.start(OFF, lambda s: calls.append(1) or (False, 'x'))
        self.run_send(o)
        self.assertEqual((len(gmail.sent), calls), (4, []))


class DailyGuardTests(DailyBase):
    def start(self, track, probe):
        sheets, gmail = FakeSheetsRW(sheet_data()), CampaignGmail()
        o = self.outreach(sheets, gmail)
        o.track, o.tracking_probe = track, probe
        return o, gmail

    def run_it(self, o, **kw):
        from services.daily import run_daily
        from tests.test_outreach import MAPPING
        return run_daily(o, MAPPING, self.lock, self.daily_log, say=self.say, ask=self.ask_with('SEND 4'),
                         lock_timeout=0.2, **kw)

    def test_report_shows_the_address_health(self):
        o, _ = self.start(CLICKS, DOWN)
        self.run_it(o)
        self.assertIn('tracking is ON (opens + clicks); tracking address: NOT REACHABLE', self.text)
        self.lines.clear()
        o, _ = self.start(CLICKS, UP)
        self.run_it(o)
        self.assertIn('tracking address: reachable', self.text)
        self.lines.clear()
        o, _ = self.start(OFF, UP)
        self.run_it(o)
        self.assertIn('tracking is OFF', self.text)

    def test_send_is_refused_when_clicks_are_tracked_and_the_address_is_down(self):
        o, gmail = self.start(CLICKS, DOWN)
        result = self.run_it(o, send=True, locks_open=True)
        self.assertEqual(result['status'], 'TRACKING_DOWN')
        self.assertIn('NOT SENDING', self.text)
        self.assertEqual((gmail.attempts, self.prompts), ([], []))          # no prompt either

    def test_opens_only_still_sends_with_a_warning_in_the_report(self):
        o, gmail = self.start(OPENS_ONLY, DOWN)
        result = self.run_it(o, send=True, locks_open=True)
        self.assertEqual((result['status'], len(gmail.sent)), ('SENT', 4))
        self.assertIn('opens will not be recorded until it is fixed', self.text)


if __name__ == '__main__':
    import unittest
    unittest.main()
