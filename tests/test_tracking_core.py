"""Mocked tests: status policy, settings, tracking store, HTML builder, endpoint, reports.

Nothing here touches Gmail, Google Sheets or the network (the endpoint test binds 127.0.0.1 only).
"""
import http.client
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from services import status_policy as sp
from services.reports import build_report, format_report
from services.tracking_injector import build_tracked_html
from services.tracking_server import RateLimiter, make_server
from services.tracking_settings import TrackingSettings, load_settings
from services.tracking_store import TrackingStore, classify_fetch

T0 = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def make_store(clock=None, min_delay=20):
    path = os.path.join(tempfile.mkdtemp(), 'tracking.db')
    store = TrackingStore(path, clock=clock or Clock(), min_delay=min_delay)
    store.init()
    return store


def send(store, key='A|x@y.com', tab='A', row=2, to='x@y.com', campaign='camp', tracking=True, gid='g1', tid='t1'):
    state, msg = store.reserve_send(key, tab, row, to, campaign, 'subject', tracking)
    assert state == 'reserved', state
    store.finalize_sent(msg['id'], gid, tid)
    return store.get_message(msg['id'])


class PolicyTests(unittest.TestCase):
    def test_allowed_moves(self):
        ok = [('', sp.SENT), ('', sp.OPENED), ('', sp.BOUNCED), ('', sp.RESPONDED),
              (sp.SENT, sp.OPENED), (sp.SENT, sp.BOUNCED), (sp.SENT, sp.RESPONDED), (sp.SENT, sp.BOUNCE_TEMPORARY),
              (sp.BOUNCE_TEMPORARY, sp.OPENED), (sp.BOUNCE_TEMPORARY, sp.BOUNCED), (sp.OPENED, sp.RESPONDED)]
        for cur, new in ok:
            self.assertTrue(sp.may_write(cur, new), (cur, new))

    def test_statuses_never_regress(self):
        bad = [(sp.OPENED, sp.SENT), (sp.RESPONDED, sp.OPENED), (sp.RESPONDED, sp.SENT), (sp.BOUNCED, sp.SENT),
               (sp.BOUNCED, sp.OPENED), (sp.BOUNCED, sp.RESPONDED), (sp.OPENED, sp.BOUNCED),
               (sp.OPENED, sp.BOUNCE_TEMPORARY), (sp.SENT, sp.SENT), (sp.RESPONDED, sp.BOUNCED)]
        for cur, new in bad:
            self.assertFalse(sp.may_write(cur, new), (cur, new))

    def test_unknown_and_no_recipient_never_replaced_or_written(self):
        for cur in ('WEIRD', sp.NO_RECIPIENT, 'custom value'):
            for new in sp.WRITABLE:
                self.assertFalse(sp.may_write(cur, new), (cur, new))
        self.assertFalse(sp.may_write('', sp.NO_RECIPIENT))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(sp.may_write(' email_sent ', 'email_opened'))
        self.assertFalse(sp.may_write('Responded', 'EMAIL_OPENED'))

    def test_conflicts_flagged(self):
        self.assertTrue(sp.is_conflict(sp.BOUNCED, sp.RESPONDED))
        self.assertTrue(sp.is_conflict(sp.OPENED, sp.BOUNCED))
        self.assertFalse(sp.is_conflict(sp.SENT, sp.OPENED))
        self.assertFalse(sp.is_conflict('WEIRD', sp.OPENED))

    def test_desired_status_priority(self):
        d = lambda **k: sp.desired_status(**{'replied': False, 'bounced_permanent': False, 'engaged': False,
                                             'bounced_temporary': False, **k})
        self.assertEqual(d(), sp.SENT)
        self.assertEqual(d(bounced_temporary=True), sp.BOUNCE_TEMPORARY)
        self.assertEqual(d(engaged=True, bounced_temporary=True), sp.OPENED)
        self.assertEqual(d(engaged=True, bounced_permanent=True), sp.BOUNCED)
        self.assertEqual(d(replied=True, bounced_permanent=True, engaged=True), sp.RESPONDED)


class SettingsTests(unittest.TestCase):
    def test_everything_off_by_default(self):
        s = load_settings({})
        self.assertFalse(s.enabled)
        self.assertEqual(s.host, '127.0.0.1')
        self.assertFalse(s.trust_proxy)
        self.assertFalse(s.count_suspect_opens)

    def test_enabled_requires_https_base_url(self):
        with self.assertRaises(ValueError):
            load_settings({'TRACKING_ENABLED': 'true'})
        with self.assertRaises(ValueError):
            load_settings({'TRACKING_ENABLED': 'true', 'TRACKING_BASE_URL': 'http://track.example.com'})
        with self.assertRaises(ValueError):
            load_settings({'TRACKING_ENABLED': 'true', 'TRACKING_BASE_URL': 'https://u:p@track.example.com'})
        ok = load_settings({'TRACKING_ENABLED': 'true', 'TRACKING_BASE_URL': 'https://track.example.com/'})
        self.assertEqual(ok.base_url, 'https://track.example.com')
        load_settings({'TRACKING_ENABLED': 'true', 'TRACKING_BASE_URL': 'http://localhost:8787'})

    def test_invalid_url_ignored_while_disabled(self):
        self.assertFalse(load_settings({'TRACKING_BASE_URL': 'not a url'}).enabled)


class StoreTests(unittest.TestCase):
    def test_reserve_blocks_second_claim(self):
        store = make_store()
        s1, m1 = store.reserve_send('A|x@y.com', 'A', 2, 'X@Y.com', 'c', 'subj', True)
        s2, m2 = store.reserve_send('A|x@y.com', 'A', 2, 'x@y.com', 'c', 'subj', True)
        self.assertEqual((s1, s2), ('reserved', 'sending'))
        self.assertEqual(m1['id'], m2['id'])
        self.assertEqual(m1['recipient'], 'x@y.com')

    def test_sent_record_has_all_fields(self):
        store = make_store()
        m = send(store, key='Tab 1|a@b.com', tab='Tab 1', row=7, to='a@b.com', campaign='camp1', gid='gm', tid='th')
        self.assertEqual((m['state'], m['tab'], m['row_no'], m['campaign'], m['recipient'],
                          m['gmail_message_id'], m['gmail_thread_id'], m['sent_at']),
                         ('sent', 'Tab 1', 7, 'camp1', 'a@b.com', 'gm', 'th', T0.isoformat()))

    def test_release_uncertain_and_resolve(self):
        store = make_store()
        _, m = store.reserve_send('k1', 'A', 1, 'a@b.com', 'c', 's', False)
        store.release(m['id'])
        self.assertIsNone(store.get_message_by_key('k1'))
        _, m = store.reserve_send('k2', 'A', 1, 'b@b.com', 'c', 's', False)
        store.mark_uncertain(m['id'])
        self.assertEqual(store.reserve_send('k2', 'A', 1, 'b@b.com', 'c', 's', False)[0], 'uncertain')
        self.assertEqual(len(store.uncertain_messages()), 1)
        self.assertTrue(store.resolve('k2', 'unsent'))
        self.assertEqual(store.reserve_send('k2', 'A', 1, 'b@b.com', 'c', 's', False)[0], 'reserved')

    def test_sent_rows_cannot_be_released_or_resolved_away(self):
        store = make_store()
        m = send(store)
        store.release(m['id'])
        self.assertEqual(store.get_message(m['id'])['state'], 'sent')
        self.assertFalse(store.resolve(m['send_key'], 'unsent'))

    def test_tracking_ids_are_unpredictable(self):
        store = make_store()
        ids = []
        for i in range(200):
            _, m = store.reserve_send(f'k{i}', 'A', i, 'same@x.com', 'c', 's', True)
            ids.append(m['tracking_id'])
        self.assertEqual(len(set(ids)), 200)
        for t in ids:
            self.assertRegex(t, r'^[A-Za-z0-9_-]{22,}$')
            self.assertNotIn('same', t)
        self.assertEqual(len({t[:4] for t in ids}) > 100, True)  # no shared prefix / counter

    def test_legacy_import_is_idempotent(self):
        store = make_store()
        path = os.path.join(tempfile.mkdtemp(), 'tracking.json')
        with open(path, 'w') as f:
            f.write('{"01 - HR|a@b.com": "mid1", "03 - Spec|c@d.com": "mid2"}')
        self.assertEqual(store.import_legacy(path), 2)
        self.assertEqual(store.import_legacy(path), 0)
        rows = store.message_rows(state='sent')
        self.assertEqual({(r['tab'], r['recipient'], r['legacy'], r['tracking_enabled']) for r in rows},
                         {('01 - HR', 'a@b.com', 1, 0), ('03 - Spec', 'c@d.com', 1, 0)})

    def test_open_events_first_latest_total_and_dedupe(self):
        clock = Clock()
        store = make_store(clock)
        m = send(store)
        clock.advance(60)
        self.assertEqual(store.record_open(m['tracking_id'], '1.1.1.1', 'Mozilla/5.0'), 'recorded')
        self.assertEqual(store.record_open(m['tracking_id'], '1.1.1.1', 'Mozilla/5.0'), 'duplicate')
        clock.advance(11)
        self.assertEqual(store.record_open(m['tracking_id'], '1.1.1.1', 'Mozilla/5.0'), 'recorded')
        clock.advance(100)
        self.assertEqual(store.record_open(m['tracking_id'], '2.2.2.2', 'Other'), 'recorded')
        row = store.message_rows()[0]
        self.assertEqual((row['opens_total'], row['opens_real']), (3, 3))
        self.assertEqual(row['first_open'], (T0 + timedelta(seconds=60)).isoformat())
        self.assertEqual(row['last_open'], (T0 + timedelta(seconds=60 + 11 + 100)).isoformat())

    def test_suspect_opens_not_counted_as_real(self):
        clock = Clock()
        store = make_store(clock)
        m = send(store)
        clock.advance(3)  # far too soon after sending
        self.assertEqual(store.record_open(m['tracking_id'], 'a', 'Mozilla'), 'recorded_suspect')
        clock.advance(300)
        self.assertEqual(store.record_open(m['tracking_id'], 'b', 'Barracuda Link Scanner'), 'recorded_suspect')
        row = store.message_rows()[0]
        self.assertEqual((row['opens_total'], row['opens_real'], row['first_open']), (2, 0, None))

    def test_classify_fetch(self):
        self.assertEqual(classify_fetch('Mozilla', 600, 20), [])
        self.assertEqual(classify_fetch('python-requests/2', 600, 20), ['scanner_user_agent'])
        self.assertEqual(classify_fetch('Mozilla', 5, 20), ['too_soon_after_send'])
        self.assertEqual(classify_fetch('Mozilla', None, 20), [])

    def test_unknown_ids_record_nothing(self):
        store = make_store()
        send(store)
        self.assertEqual(store.record_open('does-not-exist-0123456789', 'a', 'b'), 'unknown')
        self.assertEqual(store.record_click('nope', 'a', 'b'), 'unknown')
        self.assertEqual(store.message_rows()[0]['opens_total'], 0)

    def test_clicks_per_link_with_dedupe(self):
        clock = Clock()
        store = make_store(clock)
        m = send(store)
        l1 = store.add_link(m['id'], 'https://a.example/x')
        l2 = store.add_link(m['id'], 'https://b.example/y')
        self.assertEqual(store.add_link(m['id'], 'https://a.example/x'), l1)
        self.assertNotEqual(l1, l2)
        clock.advance(120)
        self.assertEqual(store.record_click(l1, 'c1', 'Mozilla'), 'recorded')
        self.assertEqual(store.record_click(l1, 'c1', 'Mozilla'), 'duplicate')
        self.assertEqual(store.record_click(l2, 'c1', 'Mozilla'), 'recorded')  # different link is not a duplicate
        row = store.message_rows()[0]
        self.assertEqual((row['clicks_total'], row['clicks_real'], row['link_count']), (2, 2, 2))

    def test_add_event_is_idempotent(self):
        store = make_store()
        m = send(store)
        self.assertTrue(store.add_event(m['id'], 'reply', T0, 'gm1', {'from': 'x@y.com'}, 'reply:gm1'))
        self.assertFalse(store.add_event(m['id'], 'reply', T0, 'gm1', {'from': 'x@y.com'}, 'reply:gm1'))
        self.assertEqual(store.message_rows()[0]['replies'], 1)

    def test_blocked_reason_spans_tabs(self):
        store = make_store()
        a = send(store, key='A|x@y.com', tab='A')
        b = send(store, key='B|x@y.com', tab='B', gid='g2')
        c = send(store, key='A|z@y.com', tab='A', to='z@y.com', gid='g3')
        self.assertIsNone(store.blocked_reason('x@y.com'))
        store.add_event(a['id'], 'bounce', T0, 'n1', {}, 'bounce:n1:x')
        self.assertEqual(store.blocked_reason('X@y.com'), 'BOUNCED')
        store.add_event(b['id'], 'reply', T0, 'r1', {}, 'reply:r1')
        self.assertEqual(store.blocked_reason('x@y.com'), 'REPLIED')
        self.assertIsNone(store.blocked_reason('z@y.com'))
        store.add_event(c['id'], 'bounce_temp', T0, 'n2', {}, 'bounce_temp:n2:z')
        self.assertIsNone(store.blocked_reason('z@y.com'))  # temporary failures do not block

    def test_actor_hash_is_salted_and_not_the_address(self):
        a, b = make_store(), make_store()
        self.assertNotEqual(a.actor_hash('9.9.9.9'), b.actor_hash('9.9.9.9'))
        self.assertNotIn('9.9.9.9', a.actor_hash('9.9.9.9'))

    def test_user_agent_is_truncated_and_cleaned(self):
        clock = Clock()
        store = make_store(clock)
        m = send(store)
        clock.advance(60)
        store.record_open(m['tracking_id'], 'a', 'Mozilla\x00\n' + 'x' * 500)
        import sqlite3
        ua = sqlite3.connect(store.db_path).execute('SELECT ua FROM events').fetchone()[0]
        self.assertEqual(len(ua), 200)
        self.assertNotRegex(ua, r'[\x00-\x1f]')


class InjectorTests(unittest.TestCase):
    TEXT = ("Hi Sam,\n\nSee https://example.com/a, and https://www.linkedin.com/in/me/.\n"
            "Mail me: me@x.com & <b>not bold</b>\n\nAdarsh · 7362973003 · https://www.linkedin.com/in/me/")

    def setUp(self):
        self.store = make_store()
        self.msg = send(self.store)

    def test_visible_text_is_unchanged(self):
        html_doc, tracked = build_tracked_html(self.TEXT, self.msg, self.store, 'https://t.example.com')
        visible = re.sub(r'<img[^>]*>', '', html_doc)
        visible = visible.replace('<br>\n', '\n')
        visible = re.sub(r'<a href="[^"]*">', '', visible).replace('</a>', '')
        visible = re.sub(r'</?div[^>]*>', '', visible).strip()
        import html
        self.assertEqual(html.unescape(visible), self.TEXT)

    def test_only_body_urls_are_wrapped_and_originals_hidden_behind_ids(self):
        html_doc, tracked = build_tracked_html(self.TEXT, self.msg, self.store, 'https://t.example.com/')
        self.assertEqual(tracked, ['https://example.com/a', 'https://www.linkedin.com/in/me/',
                                   'https://www.linkedin.com/in/me/'])
        hrefs = re.findall(r'href="([^"]+)"', html_doc)
        self.assertEqual(len(hrefs), 3)
        for h in hrefs:
            self.assertRegex(h, r'^https://t\.example\.com/c/[A-Za-z0-9_-]{8,}$')
        self.assertEqual(hrefs[1], hrefs[2])  # same URL in the same message -> same link id
        self.assertNotIn('mailto:', html_doc)
        self.assertIn('&lt;b&gt;not bold&lt;/b&gt;', html_doc)  # body is escaped, never interpreted

    def test_pixel_uses_message_tracking_id(self):
        html_doc, _ = build_tracked_html(self.TEXT, self.msg, self.store, 'https://t.example.com')
        self.assertIn(f'src="https://t.example.com/o/{self.msg["tracking_id"]}.gif"', html_doc)

    def test_link_domain_allowlist(self):
        _, tracked = build_tracked_html(self.TEXT, self.msg, self.store, 'https://t.example.com',
                                        link_domains=('linkedin.com',))
        self.assertEqual(set(tracked), {'https://www.linkedin.com/in/me/'})

    def test_link_targets_are_stored_server_side(self):
        _, _ = build_tracked_html(self.TEXT, self.msg, self.store, 'https://t.example.com')
        row = self.store.message_rows()[0]
        self.assertEqual(row['link_count'], 2)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = make_store(self.clock)
        self.msg = send(self.store)
        self.link = self.store.add_link(self.msg['id'], 'https://www.linkedin.com/in/me/')
        self.clock.advance(3600)
        self.settings = TrackingSettings(rate_limit_per_min=1000)
        self.start()

    def start(self, settings=None, limiter=None):
        self.server = make_server(self.store, settings or self.settings, host='127.0.0.1', port=0, limiter=limiter)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, method, path, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), body

    def test_pixel_records_open_and_is_uncacheable(self):
        st, h, body = self.req('GET', f"/o/{self.msg['tracking_id']}.gif", {'User-Agent': 'Mozilla/5.0'})
        self.assertEqual((st, h['content-type']), (200, 'image/gif'))
        self.assertTrue(body.startswith(b'GIF89a'))
        self.assertIn('no-store', h['cache-control'])
        self.assertEqual(self.store.message_rows()[0]['opens_real'], 1)

    def test_unknown_and_valid_ids_look_identical(self):
        a = self.req('GET', f"/o/{self.msg['tracking_id']}.gif")
        b = self.req('GET', '/o/' + 'A' * 22 + '.gif')
        self.assertEqual((a[0], a[1]['content-type'], a[2]), (b[0], b[1]['content-type'], b[2]))
        self.assertEqual(self.store.message_rows()[0]['opens_total'], 1)  # only the valid one recorded

    def test_click_redirects_to_stored_url_and_records(self):
        st, h, _ = self.req('GET', f'/c/{self.link}', {'User-Agent': 'Mozilla/5.0'})
        self.assertEqual((st, h['location']), (302, 'https://www.linkedin.com/in/me/'))
        self.assertEqual(self.store.message_rows()[0]['clicks_real'], 1)

    def test_no_open_redirect_via_parameters(self):
        st, h, _ = self.req('GET', f'/c/{self.link}?url=https://evil.example/')
        self.assertEqual(h['location'], 'https://www.linkedin.com/in/me/')
        st, h, _ = self.req('GET', '/c/unknownlinkid?url=https://evil.example/')
        self.assertEqual(st, 404)
        self.assertNotIn('location', h)

    def test_head_requests_record_nothing(self):
        self.req('HEAD', f"/o/{self.msg['tracking_id']}.gif")
        st, h, _ = self.req('HEAD', f'/c/{self.link}')
        self.assertEqual(st, 302)
        row = self.store.message_rows()[0]
        self.assertEqual((row['opens_total'], row['clicks_total']), (0, 0))

    def test_other_paths_and_long_paths(self):
        self.assertEqual(self.req('GET', '/')[0], 404)
        self.assertEqual(self.req('GET', '/o/short.gif')[0], 404)
        self.assertEqual(self.req('GET', '/x' * 200)[0], 414)
        self.assertEqual(self.req('GET', '/healthz')[0], 200)

    def test_rate_limit(self):
        self.server.shutdown()
        self.server.server_close()
        self.start(TrackingSettings(rate_limit_per_min=3))
        codes = [self.req('GET', f"/o/{self.msg['tracking_id']}.gif")[0] for _ in range(5)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3:], [429, 429])

    def test_store_failures_never_break_pixel_or_redirect(self):
        def boom(*a, **k):
            raise RuntimeError('db locked')
        self.store.record_open = boom
        self.store.record_click = boom
        self.assertEqual(self.req('GET', f"/o/{self.msg['tracking_id']}.gif")[0], 200)
        self.assertEqual(self.req('GET', f'/c/{self.link}')[0], 302)

    def test_proxy_header_ignored_unless_trusted(self):
        self.req('GET', f"/o/{self.msg['tracking_id']}.gif", {'X-Forwarded-For': '5.5.5.5', 'User-Agent': 'M'})
        self.req('GET', f"/o/{self.msg['tracking_id']}.gif", {'X-Forwarded-For': '6.6.6.6', 'User-Agent': 'M'})
        # both come from 127.0.0.1, so the second is a duplicate of the first (not trusted -> same client)
        self.assertEqual(self.store.message_rows()[0]['opens_total'], 1)


class RateLimiterTests(unittest.TestCase):
    def test_refills_over_time(self):
        t = [0.0]
        rl = RateLimiter(60, clock=lambda: t[0])
        self.assertTrue(all(rl.allow('a') for _ in range(60)))
        self.assertFalse(rl.allow('a'))
        self.assertTrue(rl.allow('b'))  # other clients unaffected
        t[0] += 2.0
        self.assertTrue(rl.allow('a'))


class ReportTests(unittest.TestCase):
    def build(self):
        clock = Clock()
        store = make_store(clock)
        a = send(store, key='A|a@x.com', tab='A', to='a@x.com', campaign='c1', tracking=True, gid='1')
        b = send(store, key='A|b@x.com', tab='A', to='b@x.com', campaign='c1', tracking=True, gid='2')
        c = send(store, key='B|c@x.com', tab='B', to='c@x.com', campaign='c2', tracking=False, gid='3')
        d = send(store, key='B|d@x.com', tab='B', to='d@x.com', campaign='c2', tracking=True, gid='4')
        link = store.add_link(a['id'], 'https://l.example/')
        clock.advance(300)
        store.record_open(a['tracking_id'], 'x', 'Mozilla')
        store.record_open(a['tracking_id'], 'y', 'Mozilla')
        store.record_click(link, 'x', 'Mozilla')
        store.record_open(b['tracking_id'], 'z', 'Barracuda')  # scanner -> raw only
        store.add_event(c['id'], 'reply', clock(), 'r1', {}, 'reply:r1')
        store.add_event(d['id'], 'bounce', clock(), 'n1', {}, 'bounce:n1:d')
        return store

    def test_counts_and_rates(self):
        store = self.build()
        store.meta_set('last_bounce_ingest', 'now')
        store.meta_set('last_reply_ingest', 'now')
        table, notes = build_report(store)
        r = table[0]
        self.assertEqual((r['sent'], r['tracked'], r['untracked']), (4, 3, 1))
        self.assertEqual((r['unique_opens'], r['opens_real'], r['opens_raw']), (1, 2, 3))
        self.assertEqual((r['unique_clicks'], r['clicks_real']), (1, 1))
        self.assertEqual((r['bounced'], r['replies']), (1, 1))
        self.assertEqual(r['delivered_confirmed'], 2)       # a (open+click) and c (reply)
        self.assertEqual(r['delivered_unconfirmed'], 1)     # b: scanner open only; d bounced is excluded
        self.assertAlmostEqual(r['open_rate'], 1 / 3)
        self.assertAlmostEqual(r['click_rate'], 1.0)        # denominator = tracked messages that had a link (only 'a')
        self.assertAlmostEqual(r['bounce_rate'], 1 / 4)
        self.assertAlmostEqual(r['reply_rate'], 1 / 4)

    def test_rates_are_na_until_ingest_has_run(self):
        table, notes = build_report(self.build())
        self.assertIsNone(table[0]['bounce_rate'])
        self.assertIsNone(table[0]['reply_rate'])
        self.assertTrue(any('not ingested' in n for n in notes))
        self.assertIn('n/a', format_report(table, notes))

    def test_group_by_and_filters(self):
        store = self.build()
        table, _ = build_report(store, by='tab')
        self.assertEqual([(r['group'], r['sent']) for r in table], [('A', 2), ('B', 2), ('TOTAL', 4)])
        table, _ = build_report(store, by='campaign', campaign='c2')
        self.assertEqual([(r['group'], r['sent']) for r in table], [('c2', 2), ('TOTAL', 2)])
        table, _ = build_report(store, recipient='a@x.com')
        self.assertEqual(table[0]['sent'], 1)
        table, _ = build_report(store, by='recipient', tab='B')
        self.assertEqual([r['group'] for r in table], ['c@x.com', 'd@x.com', 'TOTAL'])
        table, _ = build_report(store, by='day')
        self.assertEqual(table[0]['group'], '2026-11-14')

    def test_date_range(self):
        store = self.build()
        self.assertEqual(build_report(store, since='2026-11-14', until='2026-11-15')[0][0]['sent'], 4)
        self.assertEqual(build_report(store, since='2026-11-15')[0], [])
        self.assertEqual(build_report(store, until='2026-11-14')[0], [])

    def test_untracked_messages_never_claim_opens(self):
        store = make_store()
        send(store, tracking=False)
        table, notes = build_report(store)
        out = format_report(table, notes)
        self.assertIsNone(table[0]['open_rate'])
        self.assertTrue(any('sent with tracking' in n for n in notes))
        self.assertIn('n/a', out)

    def test_never_claims_delivery_without_evidence(self):
        store = make_store()
        send(store)
        table, _ = build_report(store)
        self.assertEqual((table[0]['delivered_confirmed'], table[0]['delivered_unconfirmed']), (0, 1))


class ClickSwitchTests(unittest.TestCase):
    def test_clicks_can_be_switched_off_leaving_pixel_only(self):
        self.assertTrue(load_settings({}).track_clicks)
        self.assertFalse(load_settings({'TRACKING_CLICKS': 'false'}).track_clicks)
        store = make_store()
        msg = send(store)
        text = 'Hi\nSee https://www.linkedin.com/in/me/'
        doc, tracked = build_tracked_html(text, msg, store, 'https://t.example.com', track_links=False)
        self.assertEqual(tracked, [])
        self.assertNotIn('<a ', doc)
        self.assertIn('https://www.linkedin.com/in/me/', doc)   # left exactly as written
        self.assertIn('/o/' + msg['tracking_id'] + '.gif', doc)  # open pixel still present
        self.assertEqual(store.message_rows()[0]['link_count'], 0)


if __name__ == '__main__':
    unittest.main()
