"""Detect replies in Gmail and (optionally) mark them in the sheet's Merge status column.

Read-only on Gmail. Sheet writes happen only with apply=True, only to the single
Merge status cell, and only if that cell still holds one of the "check" statuses.
"""
import re

from services.logger import get_logger
from services.outreach_service import EMAIL_RE, _norm

logger = get_logger(__name__)

AUTO_SUBJECT = re.compile(
    r'(auto(matic)?[- ]?reply|autoreply|out of office|undeliverable|delivery status|'
    r'mail delivery|delivery failure|returned mail)', re.I)


def is_automatic(headers):
    """True for out-of-office / bounce / bulk mail, so only human replies count."""
    if headers.get('auto-submitted', 'no').strip().lower() != 'no':
        return True
    if headers.get('precedence', '').strip().lower() in ('bulk', 'auto_reply', 'junk', 'list'):
        return True
    if headers.get('x-autoreply'):
        return True
    if 'mailer-daemon' in headers.get('from', '').lower() or 'postmaster' in headers.get('from', '').lower():
        return True
    return bool(AUTO_SUBJECT.search(headers.get('subject', '')))


class ReplyService:
    def __init__(self, sheets, gmail, cfg, sheet_id, days=45):
        self.sheets, self.gmail, self.cfg = sheets, gmail, cfg
        self.sheet_id, self.days = sheet_id, days
        self.check_statuses = {s.casefold() for s in cfg.get('reply_check_statuses', ['EMAIL_SENT', 'EMAIL_OPENED'])}
        self.replied_value = cfg.get('replied_value', 'RESPONDED')

    def check_all(self, apply=False):
        results = []
        actual = self.sheets.list_tabs(self.sheet_id)
        for tab, tcfg in self.cfg['tabs'].items():
            if tab not in actual:
                continue
            results.extend(self._check_tab(tab, tcfg, apply))
        return results

    def _check_tab(self, tab, tcfg, apply):
        values = self.sheets.get_tab_values(self.sheet_id, tab)
        hrow = self.cfg['header_row']
        if len(values) < hrow:
            return []
        index = {}
        for i, h in enumerate(values[hrow - 1]):
            index.setdefault(_norm(h), i)
        email_col = index.get(_norm(tcfg['email_header']))
        status_col = index.get(_norm(self.cfg['merge_status_header']))
        if email_col is None or status_col is None:
            logger.warning(f"[{tab}] no email/Merge status column; skipped")
            return []
        out = []
        for row_no, row in enumerate(values[hrow:], start=hrow + 1):
            cell = lambda c: str(row[c]).strip() if c < len(row) else ''
            status, to = cell(status_col), cell(email_col)
            if status.casefold() not in self.check_statuses or not EMAIL_RE.match(to):
                continue
            res = {'tab': tab, 'row': row_no, 'to': to, 'status': status}
            out.append(res)
            sent_ms = self.gmail.first_sent_ms(to, self.days)
            if sent_ms is None:
                res['result'] = 'NO_SENT_COPY'  # cannot prove a reply is to our email
                continue
            inbound = [m for m in self.gmail.find_replies(to, self.days) if m['internalDate'] > sent_ms]
            human = [m for m in inbound if not is_automatic(m['headers'])]
            if human:
                res['result'] = 'REPLIED'
                res['thread'] = human[0].get('threadId')
            else:
                res['result'] = 'AUTO_REPLY_ONLY' if inbound else 'NO_REPLY'
            if apply and res['result'] == 'REPLIED':
                res['result'] = self._mark(tab, status_col, row_no)
        return out

    def _mark(self, tab, status_col, row_no):
        a1 = f"{self.sheets.quote_tab(tab)}!{self.sheets.column_letter(status_col)}{row_no}"
        current = self.sheets.get_sheet_data(self.sheet_id, a1)
        current = (current[0][0] if current and current[0] else '').strip()
        if current.casefold() not in self.check_statuses:
            return 'REPLIED_NOT_MARKED_STATUS_CHANGED'
        try:
            self.sheets.update_cell(self.sheet_id, tab, status_col, row_no, self.replied_value)
            return 'REPLIED_MARKED'
        except Exception as e:
            logger.error(f"[{tab}] row {row_no}: could not mark reply: {e}")
            return 'REPLIED_MARK_FAILED'
