"""Tab-aware outreach pipeline: sheet rows -> exact templates -> Gmail -> Merge status.

Per tab, tab_mapping.json says which template to use, which header holds the
recipient email, and (optionally) {placeholder: header} overrides. By default a
placeholder is matched to the header with the same name, ignoring case and
space/underscore differences.
"""
import json
import os
import re

from services.logger import get_logger

logger = get_logger(__name__)

EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')


def _norm(s):
    return re.sub(r'[\s_]+', '', str(s)).casefold()


def load_mapping(path='tab_mapping.json'):
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg.setdefault('header_row', 1)
    cfg.setdefault('merge_status_header', 'Merge status')
    cfg.setdefault('sent_value', 'EMAIL_SENT')
    if not cfg.get('tabs'):
        raise ValueError("tab_mapping.json has no tabs configured")
    return cfg


class OutreachService:
    # Merge status (case-insensitive) -> outcome. Blank status = eligible. Nothing here is ever rewritten.
    STATUS_OUTCOMES = {
        'email_sent': 'SKIP_ALREADY_SENT',
        'email_opened': 'SKIP_ALREADY_OPENED',
        'bounced': 'FLAG_BOUNCED',
        'no_recipient': 'SKIP_NO_RECIPIENT',
    }

    def __init__(self, sheets, gmail, templates, duplicates, tracking, cfg, sheet_id,
                 resume_path=None, send_enabled=False):
        self.sheets, self.gmail, self.templates = sheets, gmail, templates
        self.dup, self.tracking, self.cfg = duplicates, tracking, cfg
        self.sheet_id, self.resume_path, self.send_enabled = sheet_id, resume_path, send_enabled
        self._tpl = {}
        self.only = None  # optional (tab, row_number): process just that one row

    # -- templates -------------------------------------------------------
    def _template(self, name):
        if name not in self._tpl:
            body = self.templates.load_template(name, 'txt')
            subject = self.templates.load_template(f"{name}_subject", 'txt').strip('\r\n')
            self._tpl[name] = (subject, body)
        return self._tpl[name]

    # -- per tab ---------------------------------------------------------
    def process_all(self):
        actual = self.sheets.list_tabs(self.sheet_id)
        results = []
        for tab in self.cfg['tabs']:
            if self.only and tab != self.only[0]:
                continue
            if tab not in actual:
                logger.error(f"Configured tab {tab!r} not found in spreadsheet (found: {actual})")
                results.append({'tab': tab, 'status': 'TAB_NOT_FOUND'})
                continue
            results.extend(self.process_tab(tab))
        for tab in actual:
            if tab not in self.cfg['tabs']:
                logger.warning(f"Tab {tab!r} has no template mapping; ignored")
        return results

    def process_tab(self, tab):
        tcfg = self.cfg['tabs'][tab]
        subject_t, body_t = self._template(tcfg['template'])
        needed = self.templates.placeholders(subject_t) + [
            p for p in self.templates.placeholders(body_t)
            if p not in self.templates.placeholders(subject_t)]

        values = self.sheets.get_tab_values(self.sheet_id, tab)
        hrow = self.cfg['header_row']
        if len(values) < hrow:
            logger.warning(f"[{tab}] no header row")
            return []
        headers = values[hrow - 1]
        index = {}
        for i, h in enumerate(headers):
            index.setdefault(_norm(h), i)

        def col(header):
            return index.get(_norm(header))

        email_col = col(tcfg['email_header'])
        status_col = col(self.cfg['merge_status_header'])
        fmap = tcfg.get('field_map', {})
        cols = {p: col(fmap.get(p, p)) for p in needed}
        problems = []
        if email_col is None:
            problems.append(f"email header {tcfg['email_header']!r}")
        if status_col is None:
            problems.append(f"status header {self.cfg['merge_status_header']!r}")
        problems += [f"header for placeholder {{{{{p}}}}}" for p, c in cols.items() if c is None]
        if problems:
            logger.error(f"[{tab}] cannot process, missing: {problems}")
            return [{'tab': tab, 'status': 'TAB_CONFIG_ERROR', 'missing': problems}]

        out = []
        for offset, row in enumerate(values[hrow:], start=hrow + 1):  # offset == sheet row number
            cell = lambda c: row[c].strip() if c < len(row) and isinstance(row[c], str) else (
                str(row[c]).strip() if c < len(row) else '')
            res = {'tab': tab, 'row': offset}
            if self.only and (tab, offset) != self.only:
                continue
            out.append(res)
            if not any(str(x).strip() for x in row):
                res['status'] = 'SKIP_BLANK'
                continue
            to = cell(email_col)
            res['to'] = to
            # Existing Merge status decides first; any non-blank value is preserved, never overwritten.
            current = cell(status_col)
            if current:
                outcome = self.STATUS_OUTCOMES.get(current.casefold())
                if outcome is None:
                    outcome = 'SKIP_UNKNOWN_STATUS'
                    logger.warning(f"[{tab}] row {offset}: unrecognised Merge status {current!r}; left untouched")
                elif outcome == 'FLAG_BOUNCED':
                    logger.warning(f"[{tab}] row {offset}: {to!r} BOUNCED - manual review needed, not resending")
                res.update(status=outcome, detail=current)
                continue
            if not EMAIL_RE.match(to):
                res.update(status='SKIP_INVALID_EMAIL', detail=to)
                logger.warning(f"[{tab}] row {offset}: invalid recipient {to!r}")
                continue
            key = f"{tab}|{to.casefold()}"
            if self.dup.is_sent(key):
                # Sent earlier but the sheet update failed: fix the cell, never resend.
                res['status'] = self._write_status(tab, status_col, offset, res, reconcile=True)
                continue
            vals = {p: cell(c) for p, c in cols.items()}
            subject, miss1 = self.templates.render_strict(subject_t, vals)
            body, miss2 = self.templates.render_strict(body_t, vals)
            if miss1 or miss2:
                missing = (miss1 or []) + (miss2 or [])
                res.update(status='SKIP_MISSING_FIELDS', missing=missing)
                logger.warning(f"[{tab}] row {offset}: missing {missing}")
                continue
            attach = self.resume_path if tcfg.get('attach_resume') else None
            res.update(subject=subject, body=body, attach=attach,
                       placeholders={p: vals[p] for p in needed})
            if not self.send_enabled:
                res['status'] = 'PREVIEW'
                continue
            try:
                resp = self.gmail.send_email(to, subject, body, attach, subtype='plain')
                if not resp or not resp.get('id'):
                    raise RuntimeError(f"Gmail returned no message id: {resp!r}")
            except Exception as e:
                res.update(status='SEND_FAILED', detail=str(e))
                logger.error(f"[{tab}] row {offset}: send failed: {e}")
                self.tracking.add_error(f"{tab} row {offset}: {e}")
                continue
            self.dup.mark_sent(key, resp['id'])
            self.tracking.increment('sent')
            res['message_id'] = resp['id']
            res['status'] = self._write_status(tab, status_col, offset, res, reconcile=False)
        return out

    def _write_status(self, tab, status_col, row_number, res, reconcile):
        try:
            self.sheets.update_cell(self.sheet_id, tab, status_col, row_number, self.cfg['sent_value'])
            return 'RECONCILED' if reconcile else 'SENT'
        except Exception as e:
            logger.error(f"[{tab}] row {row_number}: email sent but status update failed "
                         f"(will not resend; rerun to reconcile): {e}")
            res['detail'] = str(e)
            return 'SENT_STATUS_UPDATE_FAILED'

    def ensure_headers(self, tab, headers):
        """Append any missing header NAMES to the end of row `header_row` of one tab.

        Writes header cells only (never data cells), only into empty cells past
        the last existing header. Returns the headers that were added.
        """
        values = self.sheets.get_tab_values(self.sheet_id, tab)
        hrow = self.cfg['header_row']
        existing = values[hrow - 1] if len(values) >= hrow else []
        have = {_norm(h) for h in existing}
        added = []
        for h in headers:
            if _norm(h) in have:
                continue
            self.sheets.update_cell(self.sheet_id, tab, len(existing) + len(added), hrow, h)
            added.append(h)
        return added
