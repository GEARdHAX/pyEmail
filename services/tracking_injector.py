"""Build the HTML alternative of an email with a tracking pixel and tracked links.

The wording is never changed: the HTML part is the plain-text body, HTML-escaped, with
line breaks preserved as <br>. The plain-text part sent alongside it is the exact
template text. Only http(s) URLs that appear in the body are rewritten, and the visible
link text stays the original URL.
"""
import html
import re
from urllib.parse import urlparse

URL_RE = re.compile(r'https?://[^\s<>"\']+')
TRAILING = '.,;:!?)]}'


def _split_url(raw):
    """Separate trailing sentence punctuation from a matched URL."""
    url = raw
    tail = ''
    while url and url[-1] in TRAILING:
        tail = url[-1] + tail
        url = url[:-1]
    return url, tail


def _allowed(url, domains):
    if not domains:
        return True
    host = (urlparse(url).hostname or '').lower()
    return any(host == d or host.endswith('.' + d) for d in domains)


def build_tracked_html(text, message, store, base_url, link_domains=(), track_links=True):
    """Return (html, tracked_urls). `message` is the reserved messages row (dict)."""
    base = base_url.rstrip('/')
    parts, tracked, pos = [], [], 0
    for m in URL_RE.finditer(text):
        url, tail = _split_url(m.group(0))
        parts.append(html.escape(text[pos:m.start()]))
        if track_links and url and _allowed(url, link_domains) and urlparse(url).scheme in ('http', 'https'):
            link_id = store.add_link(message['id'], url)
            parts.append(f'<a href="{base}/c/{link_id}">{html.escape(url)}</a>')
            tracked.append(url)
        else:
            parts.append(html.escape(url))
        parts.append(html.escape(tail))
        pos = m.end()
    parts.append(html.escape(text[pos:]))
    body = ''.join(parts).replace('\r\n', '\n').replace('\n', '<br>\n')
    pixel = f'<img src="{base}/o/{message["tracking_id"]}.gif" width="1" height="1" alt="" border="0">'
    doc = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">'
           f'{body}</div>\n{pixel}')
    return doc, tracked
