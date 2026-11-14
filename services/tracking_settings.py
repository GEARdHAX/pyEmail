"""Tracking configuration, read from the environment (.env). Everything is OFF by default."""
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1')


def _bool(value, default=False):
    if value is None or value == '':
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


@dataclass(frozen=True)
class TrackingSettings:
    enabled: bool = False                 # open + click tracking in outgoing mail
    track_clicks: bool = True             # False = pixel only; links are left exactly as written
    base_url: str = ''                    # public URL of the tracking endpoint (https)
    db_path: str = 'data/tracking.db'
    host: str = '127.0.0.1'               # bind address of the endpoint
    port: int = 8787
    trust_proxy: bool = False             # honour X-Forwarded-For (only behind your own proxy)
    rate_limit_per_min: int = 120         # per-client requests per minute at the endpoint
    open_min_delay_seconds: int = 20      # opens/clicks sooner than this after sending are "suspect"
    count_suspect_opens: bool = False     # whether suspect opens may set EMAIL_OPENED
    link_domains: tuple = field(default_factory=tuple)  # if set, only links on these domains are tracked


def load_settings(env=None):
    env = os.environ if env is None else env
    s = TrackingSettings(
        enabled=_bool(env.get('TRACKING_ENABLED')),
        track_clicks=_bool(env.get('TRACKING_CLICKS'), True),
        base_url=(env.get('TRACKING_BASE_URL') or '').strip().rstrip('/'),
        db_path=env.get('TRACKING_DB_PATH') or 'data/tracking.db',
        host=env.get('TRACKING_HOST') or '127.0.0.1',
        port=int(env.get('TRACKING_PORT') or 8787),
        trust_proxy=_bool(env.get('TRACKING_TRUST_PROXY')),
        rate_limit_per_min=int(env.get('TRACKING_RATE_LIMIT_PER_MIN') or 120),
        open_min_delay_seconds=int(env.get('TRACKING_OPEN_MIN_DELAY_SECONDS') or 20),
        count_suspect_opens=_bool(env.get('TRACKING_COUNT_SUSPECT_OPENS')),
        link_domains=tuple(d.strip().lower() for d in (env.get('TRACKING_LINK_DOMAINS') or '').split(',') if d.strip()),
    )
    validate(s)
    return s


def validate(s):
    """Fail loudly on an unsafe configuration instead of sending broken or leaky mail."""
    if not s.enabled:
        return
    if not s.base_url:
        raise ValueError("TRACKING_ENABLED is true but TRACKING_BASE_URL is not set")
    u = urlparse(s.base_url)
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise ValueError(f"TRACKING_BASE_URL is not a valid URL: {s.base_url!r}")
    if u.scheme != 'https' and u.hostname not in LOCAL_HOSTS:
        raise ValueError("TRACKING_BASE_URL must be https (plain http is only allowed for localhost)")
    if u.query or u.fragment or u.username:
        raise ValueError("TRACKING_BASE_URL must not contain credentials, a query or a fragment")
