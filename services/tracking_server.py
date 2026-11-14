"""Tracking endpoint: 1x1 open pixel and click redirects.

Security properties
- No Google credentials and no Sheets access here; it only reads/writes the local tracking DB.
- /o/<tracking_id>.gif always answers with the same pixel (valid or unknown id), so ids
  cannot be probed; ids are 128-bit random values.
- /c/<link_id> redirects ONLY to a URL stored server-side for that link (no `?url=` parameter,
  so it cannot be abused as an open redirect). Unknown ids get a bare 404.
- Per-client token-bucket rate limit (429). HEAD requests never record events (scanners).
- Event recording can fail without breaking the pixel or the redirect.
- Raw addresses are never logged or stored; the store keeps a salted hash.
- Serve it behind HTTPS (reverse proxy / tunnel); it binds to 127.0.0.1 by default.
"""
import base64
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from services.logger import get_logger

logger = get_logger(__name__)

PIXEL = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
OPEN_RE = re.compile(r'^/o/([A-Za-z0-9_-]{16,64})\.gif$')
CLICK_RE = re.compile(r'^/c/([A-Za-z0-9_-]{8,64})$')
MAX_PATH = 200


class RateLimiter:
    """Token bucket per client key: `per_minute` sustained, burst of the same size."""

    def __init__(self, per_minute, clock=time.monotonic, max_keys=10000):
        self.rate, self.capacity, self.clock, self.max_keys = per_minute / 60.0, float(per_minute), clock, max_keys
        self._buckets, self._lock = {}, threading.Lock()

    def allow(self, key):
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            if len(self._buckets) > self.max_keys:  # drop the stalest half
                for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[: self.max_keys // 2]:
                    self._buckets.pop(k, None)
            return True


def make_handler(store, settings, limiter):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'trk'
        sys_version = ''

        def log_message(self, *args):  # default logging would print client addresses
            pass

        def _client(self):
            if settings.trust_proxy:
                xff = self.headers.get('X-Forwarded-For', '')
                if xff:
                    return xff.split(',')[-1].strip()  # the hop our own proxy appended
            return self.client_address[0]

        def _send(self, code, headers=(), body=b''):
            self.send_response(code)
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            for k, v in headers:
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def _handle(self):
            path = self.path.split('?', 1)[0]
            if len(self.path) > MAX_PATH:
                return self._send(414)
            record = self.command == 'GET'
            client = self._client()
            if path == '/healthz':
                return self._send(200, [('Content-Type', 'text/plain')], b'ok')
            if not limiter.allow(client):
                return self._send(429, [('Retry-After', '60')])
            ua = self.headers.get('User-Agent', '')

            m = OPEN_RE.match(path)
            if m:
                if record:
                    try:
                        store.record_open(m.group(1), client, ua)
                    except Exception:
                        logger.exception('open tracking failed')  # never break the pixel
                return self._send(200, [('Content-Type', 'image/gif')], PIXEL)

            m = CLICK_RE.match(path)
            if m:
                try:
                    link = store.get_link(m.group(1))
                except Exception:
                    logger.exception('link lookup failed')
                    link = None
                if not link or urlparse(link['url']).scheme not in ('http', 'https'):
                    return self._send(404, [('Content-Type', 'text/plain')], b'not found')
                if record:
                    try:
                        store.record_click(m.group(1), client, ua)
                    except Exception:
                        logger.exception('click tracking failed')  # still redirect
                return self._send(302, [('Location', link['url'])])

            return self._send(404, [('Content-Type', 'text/plain')], b'not found')

        do_GET = _handle
        do_HEAD = _handle

    return Handler


def make_server(store, settings, host=None, port=None, limiter=None):
    limiter = limiter or RateLimiter(settings.rate_limit_per_min)
    handler = make_handler(store, settings, limiter)
    return ThreadingHTTPServer((host or settings.host, settings.port if port is None else port), handler)


def serve(store, settings):  # pragma: no cover - interactive
    server = make_server(store, settings)
    host, port = server.server_address[:2]
    print(f"tracking endpoint listening on http://{host}:{port}  (put HTTPS in front of it; Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
