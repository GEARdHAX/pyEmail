"""Send ONE tracked test email to an address you name, outside the spreadsheet.

Nothing in your templates or sheet is used or changed. The message is registered in the
tracking database under tab `TEST` / campaign `tracking-test`, so its opens and clicks show
up in `track report --campaign tracking-test`. Preview unless apply=True; refuses to run
unless tracking is enabled with a valid base URL (otherwise the links would point nowhere).
"""
import secrets

from services.outreach_service import EMAIL_RE, definitely_not_sent
from services.tracking_injector import build_tracked_html

SUBJECT = 'Tracking test - please ignore'
BODY = (
    "This is a test of email tracking from pyEmail.\n\n"
    "1. Open this message (the open pixel is fetched when images load).\n"
    "2. Click this link: https://www.linkedin.com/in/adarsharya2911/\n\n"
    "Then run: python run.py track report --campaign tracking-test"
)


def send_test_email(store, gmail, settings, to, apply=False):
    to = (to or '').strip()
    if not EMAIL_RE.match(to):
        raise ValueError(f"not a valid email address: {to!r}")
    if not (settings.enabled and settings.base_url):
        raise ValueError("tracking is not enabled (set TRACKING_ENABLED=true and TRACKING_BASE_URL first)")
    result = {'to': to, 'subject': SUBJECT, 'body': BODY, 'sent': False}
    if not apply:
        result['status'] = 'PREVIEW'
        return result
    key = f"TEST|{to.lower()}|{secrets.token_hex(4)}"  # unique per test, so tests can be repeated
    _, message = store.reserve_send(key, 'TEST', None, to, 'tracking-test', SUBJECT, True)
    try:
        html_body, _ = build_tracked_html(BODY, message, store, settings.base_url, settings.link_domains,
                                           settings.track_clicks)
        resp = gmail.send_email(to, SUBJECT, BODY, None, subtype='plain', html_body=html_body)
        if not resp or not resp.get('id'):
            raise RuntimeError(f"Gmail returned no message id: {resp!r}")
    except Exception as e:
        (store.release if definitely_not_sent(e) else store.mark_uncertain)(message['id'])
        result.update(status='SEND_FAILED', detail=f"{type(e).__name__}: {e}")
        return result
    store.finalize_sent(message['id'], resp['id'], resp.get('threadId'))
    result.update(status='SENT', sent=True, message_id=resp['id'], tracking_id_prefix=message['tracking_id'][:4])
    return result
