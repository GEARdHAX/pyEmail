"""Is the public tracking address answering? Used before sending tracked email.

With click tracking on, every link in the HTML copy of an email goes through the tracking address. If that
address is down, recruiters would click a dead link. So real sends are refused until it answers.
With opens-only tracking a dead address is invisible to the recipient (the image just does not load), so a
send is allowed and only warned about.
"""
import urllib.request


class TrackingUnavailable(RuntimeError):
    """Tracked links would be broken: do not send."""


def check_tracking_endpoint(settings, timeout=10):
    """(ok, detail). Never raises. Tracking off counts as fine (there is nothing to reach)."""
    if not (settings and settings.enabled and settings.base_url):
        return True, 'tracking off'
    try:
        req = urllib.request.Request(settings.base_url.rstrip('/') + '/healthz',
                                     headers={'User-Agent': 'pyEmail-healthcheck'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(20)
            return (r.status == 200 and body.strip() == b'ok'), f'HTTP {r.status}'
    except Exception as e:
        return False, type(e).__name__   # class name only: nothing sensitive in logs


def guard_before_send(settings, probe=check_tracking_endpoint):
    """Raise TrackingUnavailable if clicks are tracked and the address is down. Returns (ok, detail)."""
    if not (settings and settings.enabled):
        return True, 'tracking off'
    ok, detail = probe(settings)
    if not ok and settings.track_clicks:
        raise TrackingUnavailable(
            f"the tracking address is not reachable ({detail}), so tracked links in the emails would be broken. "
            "Start Tailscale (and the tracking server), or set TRACKING_CLICKS=false in .env")
    return ok, detail
