"""Tab-aware outreach pipeline: sheet rows -> exact templates -> Gmail -> Merge status.

Per tab, tab_mapping.json says which template to use, which header holds the
recipient email, and (optionally) {placeholder: header} overrides. By default a
placeholder is matched to the header with the same name, ignoring case and
space/underscore differences.

Optional collaborators (all default to None, which gives the original behaviour):
- store:    TrackingStore. Reserves each send BEFORE Gmail is called, records message/thread ids,
            and blocks resends after an ambiguous failure (state `uncertain`).
- writer:   SheetStatusWriter. Verifies the row, re-reads before writing, retries transient errors.
- track:    TrackingSettings. When track.enabled, mail is sent as plain text (exact template) plus a
            tracked HTML alternative (open pixel + tracked links).

Gmail sends are NEVER retried automatically: a timeout can hide a send that succeeded, so an
ambiguous failure parks the row as `uncertain` for a human instead of risking a duplicate.
"""
import json
import re

from googleapiclient.errors import HttpError

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


def definitely_not_sent(exc):
    """True only for failures that happen before/without Gmail accepting anything."""
    if isinstance(exc, (FileNotFoundError, ValueError, TypeError)):
        return True
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, 'status', 0)
        return 400 <= status < 500 and status != 408
    return False


class OutreachService:
    # Merge status (case-insensitive) -> outcome. Blank status = eligible. Nothing here is ever rewritten.
    STATUS_OUTCOMES = {
        'email_sent': 'SKIP_ALREADY_SENT',
        'email_opened': 'SKIP_ALREADY_OPENED',
        'bounced': 'FLAG_BOUNCED',
        'email_bounced': 'FLAG_BOUNCED',
        'bounce_temporary': 'FLAG_BOUNCE_TEMPORARY',
        'responded': 'SKIP_RESPONDED',
        'email_replied': 'SKIP_RESPONDED',
        'unsubscribed': 'SKIP_UNSUBSCRIBED',
        'do_not_track': 'SKIP_DO_NOT_TRACK',
        'no_recipient': 'SKIP_NO_RECIPIENT',
    }
    # A row with one of these statuses suppresses the SAME address in every tab (replied, permanently
    # bounced, unsubscribed, invalid). A temporary bounce only flags its own row.
    BLOCKING = {'bounced', 'email_bounced', 'responded', 'email_replied', 'unsubscribed', 'do_not_track',
                'no_recipient'}

    def __init__(self, sheets, gmail, templates, duplicates, tracking, cfg, sheet_id,
                 resume_path=None, send_enabled=False, store=None, writer=None, track=None, revalidate=False):
        self.sheets, self.gmail, self.templates = sheets, gmail, templates
        self.dup, self.tracking, self.cfg = duplicates, tracking, cfg
        self.sheet_id, self.resume_path, self.send_enabled = sheet_id, resume_path, send_enabled
        self.store, self.writer, self.track = store, writer, track
        self.revalidate = revalidate   # re-read the live row + tracking DB right before every send
        self._email_col = None
        self.send_limit = None   # optional cap on Gmail send attempts per process_all() run
        self._attempted = 0
        self._tpl = {}
        self._cache, self._blocked = {}, {}
        self.only = None  # optional (tab, row_number): process just that one row

    # -- templates -------------------------------------------------------
    def _template(self, name):
        if name not in self._tpl:
            body = self.templates.load_template(name, 'txt')
            subject = self.templates.load_template(f"{name}_subject", 'txt').strip('\r\n')
            self._tpl[name] = (subject, body)
        return self._tpl[name]

    def _tracking_on(self):
        return bool(self.track and self.track.enabled and self.store)

    # -- per tab ---------------------------------------------------------
    def process_all(self):
        self._attempted = 0
        actual = self.sheets.list_tabs(self.sheet_id)
        if self.writer:
            self.writer.begin_run()
        self._cache = {tab: self.sheets.get_tab_values(self.sheet_id, tab)
                       for tab in self.cfg['tabs'] if tab in actual}
        self._blocked = self._scan_blocked()
        results = []
        try:
            for tab in self.cfg['tabs']:
                if self.only and tab != self.only[0]:
                    continue
                if tab not in actual:
                    logger.error(f"Configured tab {tab!r} not found in spreadsheet (found: {actual})")
                    results.append({'tab': tab, 'status': 'TAB_NOT_FOUND'})
                    continue
                results.extend(self.process_tab(tab))
        finally:
            self._cache = {}
        for tab in actual:
            if tab not in self.cfg['tabs']:
                logger.warning(f"Tab {tab!r} has no template mapping; ignored")
        return results

    def _header_index(self, values):
        hrow = self.cfg['header_row']
        index = {}
        if len(values) >= hrow:
            for i, h in enumerate(values[hrow - 1]):
                index.setdefault(_norm(h), i)
        return index

    def _scan_blocked(self):
        """{email: status} for every address that is BOUNCED/RESPONDED in ANY mapped tab."""
        blocked = {}
        hrow = self.cfg['header_row']
        for tab, values in self._cache.items():
            index = self._header_index(values)
            e = index.get(_norm(self.cfg['tabs'][tab]['email_header']))
            s = index.get(_norm(self.cfg['merge_status_header']))
            if e is None or s is None:
                continue
            for row in values[hrow:]:
                email = str(row[e]).strip().casefold() if e < len(row) else ''
                status = str(row[s]).strip().casefold() if s < len(row) else ''
                if email and status in self.BLOCKING:
                    blocked.setdefault(email, status.upper())
        return blocked

    def process_tab(self, tab):
        tcfg = self.cfg['tabs'][tab]
        subject_t, body_t = self._template(tcfg['template'])
        needed = self.templates.placeholders(subject_t) + [
            p for p in self.templates.placeholders(body_t)
            if p not in self.templates.placeholders(subject_t)]

        values = self._cache[tab] if tab in self._cache else self.sheets.get_tab_values(self.sheet_id, tab)
        hrow = self.cfg['header_row']
        if len(values) < hrow:
            logger.warning(f"[{tab}] no header row")
            return []
        index = self._header_index(values)

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
        self._email_col = email_col
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
            # Never contact an address that replied or bounced anywhere else (any tab, sheet or tracking DB).
            elsewhere = self._blocked.get(to.casefold())
            if not elsewhere and self.store:
                elsewhere = self.store.blocked_reason(to)
            if elsewhere:
                res.update(status='SKIP_REPLIED_ELSEWHERE' if elsewhere == 'RESPONDED' or elsewhere == 'REPLIED'
                           else 'SKIP_BOUNCED_ELSEWHERE', detail=elsewhere)
                logger.warning(f"[{tab}] row {offset}: {to!r} is {elsewhere} in another tab/record; not sending")
                continue
            key = f"{tab}|{to.casefold()}"
            if self.dup.is_sent(key):
                # Sent earlier but the sheet update failed: fix the cell, never resend.
                res['status'] = self._write_status(tab, status_col, offset, to, res, reconcile=True)
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
            res.update(subject=subject, body=body, attach=attach, tracking=self._tracking_on(),
                       placeholders={p: vals[p] for p in needed})
            if not self.send_enabled:
                res['status'] = 'PREVIEW'
                continue
            self._send_one(tab, tcfg, offset, status_col, key, to, subject, body, attach, res)
        return out

    # -- the send itself ---------------------------------------------------
    def _revalidate(self, tab, offset, to, status_col):
        """Last look before sending (fail closed). Returns None if still fine, else the skip status."""
        if not self.revalidate:
            return None
        try:
            if self.writer:
                live = self.writer._retry(self.writer._fresh_row, tab, offset)
            else:
                data = self.sheets.get_sheet_data(self.sheet_id, f"{self.sheets.quote_tab(tab)}!{offset}:{offset}")
                live = data[0] if data else []
        except Exception as e:
            logger.error(f"[{tab}] row {offset}: could not re-read the row before sending "
                         f"({type(e).__name__}); not sending")
            return 'SKIP_REVALIDATION_FAILED'
        cell = lambda c: str(live[c]).strip() if c is not None and c < len(live) else ''
        if cell(self._email_col).casefold() != to.casefold():
            return 'SKIP_ROW_CHANGED'
        status = cell(status_col)
        if status:  # something wrote a status since we read the tab (a bounce, a reply, another run...)
            return self.STATUS_OUTCOMES.get(status.casefold(), 'SKIP_STATUS_CHANGED')
        if self.store:
            reason = self.store.blocked_reason(to)
            if reason:
                return 'SKIP_REPLIED_ELSEWHERE' if reason == 'REPLIED' else 'SKIP_BOUNCED_ELSEWHERE'
        return None

    def _send_one(self, tab, tcfg, offset, status_col, key, to, subject, body, attach, res):
        if self.send_limit is not None and self._attempted >= self.send_limit:
            res.update(status='SKIP_DAILY_LIMIT')   # cap reached: the rest waits for the next run
            return
        verdict = self._revalidate(tab, offset, to, status_col)
        if verdict:
            res.update(status=verdict)
            logger.warning(f"[{tab}] row {offset}: not sending after final check ({verdict})")
            return
        message = None
        if self.store:
            state, message = self.store.reserve_send(
                key, tab, offset, to, tcfg.get('campaign') or tab, subject, self._tracking_on())
            if state == 'sent':
                res['status'] = self._write_status(tab, status_col, offset, to, res, reconcile=True)
                return
            if state != 'reserved':  # 'sending' / 'uncertain': an earlier attempt may have gone out
                res.update(status='SKIP_UNCERTAIN_PREVIOUS_ATTEMPT')
                logger.error(f"[{tab}] row {offset}: an earlier send attempt is unresolved; "
                             f"not sending (see `track uncertain` / `track resolve`)")
                return
        extra = {}
        self._attempted += 1   # counts real Gmail attempts (not skips) against send_limit
        try:
            if self._tracking_on():
                from services.tracking_injector import build_tracked_html
                extra['html_body'], _ = build_tracked_html(
                    body, message, self.store, self.track.base_url, self.track.link_domains, self.track.track_clicks)
            resp = self.gmail.send_email(to, subject, body, attach, subtype='plain', **extra)
            if not resp or not resp.get('id'):
                raise RuntimeError(f"Gmail returned no message id: {resp!r}")
        except Exception as e:
            uncertain = bool(message) and not definitely_not_sent(e)
            if message:
                (self.store.mark_uncertain if uncertain else self.store.release)(message['id'])
            res.update(status='SEND_FAILED', detail=type(e).__name__ + ': ' + str(e), uncertain=uncertain)
            logger.error(f"[{tab}] row {offset}: send failed ({'UNCERTAIN - will not retry' if uncertain else 'not sent'}): {e}")
            self.tracking.add_error(f"{tab} row {offset}: {type(e).__name__}")
            return
        self.dup.mark_sent(key, resp['id'])
        self.tracking.increment('sent')
        if message:
            try:
                self.store.finalize_sent(message['id'], resp['id'], resp.get('threadId'))
            except Exception as e:  # Gmail accepted it; the duplicate guard above already holds
                logger.error(f"[{tab}] row {offset}: sent, but could not record in tracking DB: {e}")
        res['message_id'] = resp['id']
        res['status'] = self._write_status(tab, status_col, offset, to, res, reconcile=False)

    def _write_status(self, tab, status_col, row_number, to, res, reconcile):
        try:
            if self.writer:
                r = self.writer.apply(tab, row_number, to, self.cfg['sent_value'])
                if r['result'] not in ('WRITTEN', 'ALREADY_SET'):
                    raise RuntimeError(f"sheet write not applied: {r['result']}")
            else:
                self.sheets.update_cell(self.sheet_id, tab, status_col, row_number, self.cfg['sent_value'])
            return 'RECONCILED' if reconcile else 'SENT'
        except Exception as e:
            logger.error(f"[{tab}] row {row_number}: email sent but status update failed "
                         f"(will not resend; rerun or `track sync --apply` to reconcile): {e}")
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
