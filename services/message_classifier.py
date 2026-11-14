"""Classify an inbound Gmail message from its headers (+ snippet) -- no bodies are read.

Categories, checked in this order (a delivery-failure notice is itself `Auto-Submitted`,
so bounces must be recognised before out-of-office / automated mail):

    BOUNCE_PERMANENT   delivery failed for good (5.x.x, "address not found", ...)
    BOUNCE_TEMPORARY   delayed / will keep trying (4.x.x, mailbox full, ...)
    BOUNCE_UNCLASSIFIED looks like a delivery notice but permanence cannot be told
    OUT_OF_OFFICE      auto-reply / vacation responder
    AUTOMATED          bulk, list, no-reply or system-generated mail
    HUMAN              none of the above: counts as a reply

Only HUMAN may ever become a reply. Header values may be missing or malformed; this module
never raises on bad input and falls back to HUMAN only when nothing automatic is detected.
"""
import re

HUMAN = 'human'
OUT_OF_OFFICE = 'out_of_office'
AUTOMATED = 'automated'
BOUNCE_PERMANENT = 'bounce_permanent'
BOUNCE_TEMPORARY = 'bounce_temporary'
BOUNCE_UNCLASSIFIED = 'bounce_unclassified'
BOUNCES = (BOUNCE_PERMANENT, BOUNCE_TEMPORARY, BOUNCE_UNCLASSIFIED)

DAEMON_FROM = re.compile(r'(mailer-daemon|postmaster|mail delivery (sub)?system|mail delivery agent)', re.I)
REPORT_TYPE = re.compile(r'report-type\s*=\s*"?delivery-status|multipart/report', re.I)

FAILURE_SUBJECT = re.compile(
    r'(delivery status notification|undeliverable|undelivered|returned mail|failure notice|delivery failure|'
    r'mail delivery failed|delivery (has )?failed|message not delivered|could not be delivered|delivery incomplete|'
    r'non[- ]delivery)', re.I)
DELAY_SUBJECT = re.compile(
    r'(\bdelay(ed)?\b|\bwarning\b|could not send message for past|still trying|will keep trying|'
    r'temporar(y|ily))', re.I)

PERMANENT_TEXT = re.compile(
    r'(\b5\.\d{1,3}\.\d{1,3}\b|\b55\d[ -]|user unknown|unknown user|no such user|address not found|'
    r"does not exist|doesn['’]t exist|mailbox unavailable|recipient rejected|invalid recipient|"
    r'domain (not found|does not exist)|account (is )?disabled|550 )', re.I)
TEMPORARY_TEXT = re.compile(
    r'(\b4\.\d{1,3}\.\d{1,3}\b|\b4[25]\d[ -]|mailbox (is )?full|over quota|try again later|temporar(y|ily)|'
    r'will (keep|continue) trying|greylist)', re.I)

OOO_SUBJECT = re.compile(
    r'(out of (the )?office|automatic reply|auto[- ]?reply|autoreply|\booo\b|away from (my |the )?(office|desk)|'
    r'on (annual )?(leave|vacation|holiday)|vacation (reply|responder)|currently unavailable)', re.I)
NOREPLY_FROM = re.compile(r'(^|[<\s"])(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?|'
                          r'mailer|bounce[s]?)@', re.I)


def _s(value):
    return value if isinstance(value, str) else ('' if value is None else str(value))


def bounce_kind(subject='', sender='', content_type='', snippet='', auto_submitted=''):
    """'permanent' | 'temporary' | 'unclassified' | None (not a delivery notice).

    A failure-sounding subject alone is NOT enough when a real person's address sent the
    message: it must come from a mail daemon, be a delivery-status report, be auto-submitted,
    or have an unknown sender (as for notices found by the daemon-only Gmail search).
    """
    subject, sender, content_type, snippet = _s(subject), _s(sender), _s(content_type), _s(snippet)
    auto = _s(auto_submitted).strip().lower()
    subject_says_failure = bool(FAILURE_SUBJECT.search(subject))
    looks_like_notice = bool(
        DAEMON_FROM.search(sender) or REPORT_TYPE.search(content_type)
        or (subject_says_failure and (not sender.strip() or auto not in ('', 'no'))))
    if not looks_like_notice:
        return None
    if DELAY_SUBJECT.search(subject):
        return 'temporary'
    if subject_says_failure:
        return 'permanent'
    # generic subject from a mail daemon: decide from the enhanced status code / wording in the text
    if PERMANENT_TEXT.search(snippet) and not TEMPORARY_TEXT.search(snippet):
        return 'permanent'
    if TEMPORARY_TEXT.search(snippet) and not PERMANENT_TEXT.search(snippet):
        return 'temporary'
    return 'unclassified'


def classify_message(headers, snippet=''):
    """Category for one inbound message. `headers` keys are lower-case header names."""
    h = {str(k).lower(): _s(v) for k, v in (headers or {}).items()}
    kind = bounce_kind(h.get('subject', ''), h.get('from', ''), h.get('content-type', ''), snippet,
                       h.get('auto-submitted', ''))
    if kind:
        return {'permanent': BOUNCE_PERMANENT, 'temporary': BOUNCE_TEMPORARY}.get(kind, BOUNCE_UNCLASSIFIED)

    auto = h.get('auto-submitted', 'no').strip().lower()
    if h.get('x-autoreply') or h.get('x-autorespond') or auto == 'auto-replied' or OOO_SUBJECT.search(h.get('subject', '')):
        return OUT_OF_OFFICE
    if auto not in ('', 'no'):
        return AUTOMATED
    if h.get('precedence', '').strip().lower() in ('bulk', 'junk', 'list', 'auto_reply'):
        return AUTOMATED
    if h.get('list-id') or h.get('list-unsubscribe') or h.get('x-auto-response-suppress'):
        return AUTOMATED
    if NOREPLY_FROM.search(h.get('from', '')):
        return AUTOMATED
    return HUMAN
