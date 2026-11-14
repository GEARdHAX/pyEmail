"""Merge status vocabulary and the rules for which status may replace which.

Statuses never regress: a status may only be replaced by one that is listed for it
below, and anything not listed (custom values, NO_RECIPIENT, ...) is left untouched.

    ''  (blank)        -> anything below
    EMAIL_SENT         -> BOUNCE_TEMPORARY, EMAIL_OPENED, BOUNCED, RESPONDED
    BOUNCE_TEMPORARY   -> EMAIL_OPENED (proves delivery), BOUNCED, RESPONDED
    EMAIL_OPENED       -> RESPONDED
    BOUNCED            -> (final; a later open/reply is reported as a conflict, never written)
    RESPONDED          -> (final)
    NO_RECIPIENT       -> (never written or replaced by the tracker)
"""

SENT = 'EMAIL_SENT'
OPENED = 'EMAIL_OPENED'
BOUNCED = 'BOUNCED'
NO_RECIPIENT = 'NO_RECIPIENT'
RESPONDED = 'RESPONDED'
BOUNCE_TEMPORARY = 'BOUNCE_TEMPORARY'

ALL_STATUSES = (SENT, OPENED, BOUNCED, NO_RECIPIENT, RESPONDED, BOUNCE_TEMPORARY)
WRITABLE = (SENT, OPENED, BOUNCED, RESPONDED, BOUNCE_TEMPORARY)  # NO_RECIPIENT is never written

# new status -> current statuses it may replace
_REPLACES = {
    SENT: {''},
    BOUNCE_TEMPORARY: {'', SENT},
    OPENED: {'', SENT, BOUNCE_TEMPORARY},
    BOUNCED: {'', SENT, BOUNCE_TEMPORARY},
    RESPONDED: {'', SENT, BOUNCE_TEMPORARY, OPENED},
}

# Pairs of statuses that contradict each other (mail that bounced but was opened/answered).
# Any other blocked move is just "the sheet is already ahead" and is skipped quietly.
_CONTRADICTORY = {frozenset({BOUNCED, OPENED}), frozenset({BOUNCED, RESPONDED})}


def normalize(status):
    return (status or '').strip().upper()


def may_write(current, new):
    """True if `new` is allowed to replace `current` (case-insensitive)."""
    new = normalize(new)
    return new in _REPLACES and normalize(current) in _REPLACES[new]


def is_conflict(current, new):
    """True when the two statuses contradict each other and the move is not allowed.

    Example: evidence says RESPONDED but the sheet already says BOUNCED. Needs a human look.
    """
    pair = frozenset({normalize(current), normalize(new)})
    return pair in _CONTRADICTORY and not may_write(current, new)


def desired_status(*, replied, bounced_permanent, engaged, bounced_temporary):
    """Status implied by the evidence on one message. Reply beats bounce beats open."""
    if replied:
        return RESPONDED
    if bounced_permanent:
        return BOUNCED
    if engaged:
        return OPENED
    if bounced_temporary:
        return BOUNCE_TEMPORARY
    return SENT
