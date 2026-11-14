"""Find bounced recipients in Gmail and mark matching sheet rows.

Gmail is read-only. Sheet writes happen only with apply=True, only to the single
Merge status cell of a row, and only where services.status_policy allows the move
(re-read just before writing):

    permanent failure -> BOUNCED          (over blank / EMAIL_SENT / BOUNCE_TEMPORARY)
    temporary failure -> BOUNCE_TEMPORARY (over EMAIL_SENT only; never onto a blank row)

Rows with any other status (EMAIL_OPENED, RESPONDED, NO_RECIPIENT, custom values) are
never overwritten; they are reported instead.
"""
import re

from services import status_policy as sp
from services.logger import get_logger
from services.message_classifier import bounce_kind
from services.outreach_service import EMAIL_RE, _norm

logger = get_logger(__name__)

ADDR_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PERMANENT = 'permanent'
TEMPORARY = 'temporary'


def notice_kind(notice):
    """'permanent' | 'temporary' | 'unclassified' | None for one Gmail notice dict (never raises)."""
    return bounce_kind(notice.get('subject', ''), notice.get('from', ''), notice.get('content_type', ''),
                       notice.get('snippet', ''))


def classify_notice(notice, known):
    """{address: 'permanent'|'temporary'} for the known addresses this notice is about."""
    kind = notice_kind(notice)
    if kind not in (PERMANENT, TEMPORARY):
        return {}
    found = {a.lower() for a in ADDR_RE.findall(_text(notice.get('failed')))}
    if not found:  # fall back to the notice text, but only trust addresses we actually emailed
        found = {a.lower() for a in ADDR_RE.findall(_text(notice.get('snippet')))}
    return {a: kind for a in found & known}


def _text(value):
    return value if isinstance(value, str) else ''


def failed_addresses(notice, known):
    """Addresses reported as PERMANENTLY undeliverable (kept for backwards compatibility)."""
    return {a for a, kind in classify_notice(notice, known).items() if kind == PERMANENT}


class BounceService:
    def __init__(self, sheets, gmail, cfg, sheet_id, days=45):
        self.sheets, self.gmail, self.cfg = sheets, gmail, cfg
        self.sheet_id, self.days = sheet_id, days
        self.bounced_value = cfg.get('bounced_value', sp.BOUNCED)

    def _rows(self):
        """Every sheet row that has a valid email: (tab, row_no, status_col, status, email)."""
        actual = self.sheets.list_tabs(self.sheet_id)
        hrow = self.cfg['header_row']
        for tab, tcfg in self.cfg['tabs'].items():
            if tab not in actual:
                continue
            values = self.sheets.get_tab_values(self.sheet_id, tab)
            if len(values) < hrow:
                continue
            index = {}
            for i, h in enumerate(values[hrow - 1]):
                index.setdefault(_norm(h), i)
            email_col = index.get(_norm(tcfg['email_header']))
            status_col = index.get(_norm(self.cfg['merge_status_header']))
            if email_col is None or status_col is None:
                logger.warning(f"[{tab}] no email/Merge status column; skipped")
                continue
            for row_no, row in enumerate(values[hrow:], start=hrow + 1):
                cell = lambda c: str(row[c]).strip() if c < len(row) else ''
                to = cell(email_col)
                if EMAIL_RE.match(to):
                    yield tab, row_no, status_col, cell(status_col), to

    def check_all(self, apply=False):
        rows = list(self._rows())
        known = {r[4].lower() for r in rows}
        kinds = {}
        for notice in self.gmail.find_bounces(self.days):
            for addr, kind in classify_notice(notice, known).items():
                if kinds.get(addr) != PERMANENT:  # permanent wins over temporary
                    kinds[addr] = kind
        results = []
        for tab, row_no, status_col, status, to in rows:
            kind = kinds.get(to.lower())
            if not kind:
                continue
            new = sp.BOUNCED if kind == PERMANENT else sp.BOUNCE_TEMPORARY
            res = {'tab': tab, 'row': row_no, 'to': to, 'status': status or '(blank)', 'kind': kind}
            results.append(res)
            if sp.normalize(status) == new:
                res['result'] = 'ALREADY_BOUNCED' if new == sp.BOUNCED else 'ALREADY_TEMPORARY'
            elif not self._allowed(status, new):
                res['result'] = 'LEFT_ALONE_OTHER_STATUS'
            elif not apply:
                res['result'] = 'WOULD_MARK'
            else:
                res['result'] = self._mark(tab, status_col, row_no, new)
        return results

    @staticmethod
    def _allowed(current, new):
        if not sp.may_write(current, new):
            return False
        # a temporary failure is only meaningful for mail we actually sent
        return not (new == sp.BOUNCE_TEMPORARY and not sp.normalize(current))

    def _mark(self, tab, status_col, row_no, new):
        a1 = f"{self.sheets.quote_tab(tab)}!{self.sheets.column_letter(status_col)}{row_no}"
        current = self.sheets.get_sheet_data(self.sheet_id, a1)
        current = (current[0][0] if current and current[0] else '').strip()
        if not self._allowed(current, new):
            return 'NOT_MARKED_STATUS_CHANGED'
        try:
            self.sheets.update_cell(self.sheet_id, tab, status_col, row_no,
                                    self.bounced_value if new == sp.BOUNCED else new)
            return 'MARKED'
        except Exception as e:
            logger.error(f"[{tab}] row {row_no}: could not mark bounce: {e}")
            return 'MARK_FAILED'
