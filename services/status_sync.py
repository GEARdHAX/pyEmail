"""Guarded writes of tracking results into the Google Sheet.

Rules enforced here (not left to callers):
- Only the `Merge status` cell, plus tracking columns you explicitly configured under
  `tracking_columns` in tab_mapping.json AND that already exist in the sheet, are ever written.
  Email, Campaign status and personalisation columns are never touched.
- Before a write, the row's email is verified. If rows moved, the row is re-located by email
  only when exactly one row matches; otherwise nothing is written.
- Just before each write the row is re-read, and the status transition must still be allowed
  by services.status_policy (so statuses never regress and unknown values are never replaced).
- Transient API errors (429/5xx/timeouts) are retried with exponential backoff.
"""
import socket
import time

from googleapiclient.errors import HttpError

from services import status_policy as sp
from services.logger import get_logger
from services.outreach_service import _norm

logger = get_logger(__name__)

TRANSIENT_HTTP = {429, 500, 502, 503, 504}
TRACKING_METRICS = ('first_open_at', 'open_count', 'click_count', 'replied_at')


def is_transient(exc):
    if isinstance(exc, HttpError):
        return getattr(exc.resp, 'status', None) in TRANSIENT_HTTP
    return isinstance(exc, (socket.timeout, TimeoutError, ConnectionError))


class SheetStatusWriter:
    def __init__(self, sheets, cfg, sheet_id, sleep=time.sleep, attempts=4):
        self.sheets, self.cfg, self.sheet_id = sheets, cfg, sheet_id
        self.sleep, self.attempts = sleep, attempts
        self.tracking_columns = dict(cfg.get('tracking_columns') or {})
        self._validate_tracking_columns()
        self._snap = {}

    @property
    def has_tracking_columns(self):
        return bool(self.tracking_columns)

    def _validate_tracking_columns(self):
        unknown = set(self.tracking_columns) - set(TRACKING_METRICS)
        if unknown:
            raise ValueError(f"unknown tracking_columns metrics: {sorted(unknown)}")
        protected = {_norm(self.cfg.get('merge_status_header', 'Merge status')), _norm('Campaign status')}
        for t in self.cfg['tabs'].values():
            protected.add(_norm(t['email_header']))
            for p, h in (t.get('field_map') or {}).items():
                protected.update((_norm(p), _norm(h)))
        for metric, header in self.tracking_columns.items():
            if _norm(header) in protected:
                raise ValueError(f"tracking column {header!r} for {metric} collides with a protected column")

    def _retry(self, fn, *args):
        for attempt in range(self.attempts):
            try:
                return fn(*args)
            except Exception as e:
                if not is_transient(e) or attempt == self.attempts - 1:
                    raise
                delay = 2 ** attempt
                logger.warning(f"transient Sheets error ({type(e).__name__}); retry in {delay}s")
                self.sleep(delay)

    # -- reads ---------------------------------------------------------------
    def begin_run(self):
        self._snap = {}

    def _tab(self, tab):
        """(header_index, rows) snapshot of a tab, loaded once per run."""
        if tab not in self._snap:
            values = self._retry(self.sheets.get_tab_values, self.sheet_id, tab)
            hrow = self.cfg['header_row']
            header = values[hrow - 1] if len(values) >= hrow else []
            index = {}
            for i, h in enumerate(header):
                index.setdefault(_norm(h), i)
            self._snap[tab] = (index, values)
        return self._snap[tab]

    def _columns(self, tab):
        index, _ = self._tab(tab)
        cols = {
            'email': index.get(_norm(self.cfg['tabs'][tab]['email_header'])),
            'status': index.get(_norm(self.cfg.get('merge_status_header', 'Merge status'))),
            'tracking': {m: index[_norm(h)] for m, h in self.tracking_columns.items() if _norm(h) in index},
        }
        return cols

    @staticmethod
    def _cell(row, col):
        return str(row[col]).strip() if col is not None and col < len(row) else ''

    def _locate(self, tab, row_no, email, cols):
        """Row number holding `email`, or (None, reason)."""
        _, values = self._tab(tab)
        hrow = self.cfg['header_row']
        want = email.strip().casefold()
        if row_no and row_no > hrow and row_no <= len(values) and \
                self._cell(values[row_no - 1], cols['email']).casefold() == want:
            return row_no, None
        hits = [n for n, r in enumerate(values[hrow:], start=hrow + 1)
                if self._cell(r, cols['email']).casefold() == want]
        if len(hits) == 1:
            return hits[0], None
        return None, 'ROW_NOT_FOUND' if not hits else 'ROW_AMBIGUOUS'

    def _fresh_row(self, tab, row_no):
        rng = f"{self.sheets.quote_tab(tab)}!{row_no}:{row_no}"
        data = self._retry(self.sheets.get_sheet_data, self.sheet_id, rng)
        return data[0] if data else []

    # -- the one write path ----------------------------------------------------
    def apply(self, tab, row_no, email, new_status, apply=True, tracking_values=None):
        new = sp.normalize(new_status)
        if new not in sp.WRITABLE:
            raise ValueError(f"{new_status!r} is not a status the tracker may write")
        res = {'tab': tab, 'row': row_no, 'to': email, 'new': new, 'result': None, 'current': None}
        if tab not in self.cfg['tabs']:
            res['result'] = 'TAB_NOT_MAPPED'
            return res
        cols = self._columns(tab)
        if cols['email'] is None or cols['status'] is None:
            res['result'] = 'TAB_CONFIG_ERROR'
            return res
        located, reason = self._locate(tab, row_no, email, cols)
        if located is None:
            res['result'] = reason
            return res
        res['row'] = located
        _, values = self._tab(tab)
        current = self._cell(values[located - 1], cols['status'])
        res['current'] = current
        if sp.normalize(current) == new:
            res['result'] = 'ALREADY_SET'
            return res
        if not sp.may_write(current, new):
            res['result'] = 'CONFLICT' if sp.is_conflict(current, new) else 'SKIPPED_STATUS'
            return res
        if not apply:
            res['result'] = 'WOULD_WRITE'
            return res

        # read-before-write: re-read the live row and re-check everything
        live = self._fresh_row(tab, located)
        if self._cell(live, cols['email']).casefold() != email.strip().casefold():
            res['result'] = 'ROW_CHANGED'
            return res
        live_status = self._cell(live, cols['status'])
        res['current'] = live_status
        if not sp.may_write(live_status, new):
            res['result'] = 'STATUS_CHANGED'
            return res

        cells = [(tab, cols['status'], located, new)]
        for metric, value in (tracking_values or {}).items():
            col = cols['tracking'].get(metric)
            if col is not None and value not in (None, ''):
                cells.append((tab, col, located, value))
        allowed = {cols['status'], *cols['tracking'].values()}
        assert all(c[1] in allowed for c in cells), 'refusing to write outside the approved columns'
        try:
            if len(cells) == 1:
                self._retry(self.sheets.update_cell, self.sheet_id, tab, cols['status'], located, new)
            else:
                self._retry(self.sheets.update_cells, self.sheet_id, cells)
        except Exception as e:
            logger.error(f"[{tab}] row {located}: sheet write failed: {type(e).__name__}")
            res['result'] = 'WRITE_FAILED'
            res['detail'] = type(e).__name__
            return res
        res['result'] = 'WRITTEN'
        return res


def sync_statuses(store, writer, settings, apply=False, overlay=None):
    """Push the status implied by stored evidence to the sheet (preview unless apply=True).

    `overlay` ({message_id: {'replies','bounces_perm','bounces_temp'}}) adds evidence that was
    DETECTED but not yet recorded (an ingest preview), so the proposed changes can be shown first.
    It is ignored when apply=True: only recorded events may ever be written to the sheet.
    """
    overlay = overlay if not apply else None
    writer.begin_run()
    results = []
    for m in store.message_rows(state='sent'):
        if m['tab'] not in writer.cfg['tabs']:
            continue  # e.g. the TEST message: not a spreadsheet row, nothing to sync
        pending = (overlay or {}).get(m['id'])
        if pending:
            m = {**m, **{k: m[k] + v for k, v in pending.items()}}
        engaged = m['opens_real'] > 0 or m['clicks_real'] > 0
        if settings.count_suspect_opens:
            engaged = engaged or m['opens_total'] > 0 or m['clicks_total'] > 0
        desired = sp.desired_status(replied=m['replies'] > 0, bounced_permanent=m['bounces_perm'] > 0,
                                    engaged=engaged, bounced_temporary=m['bounces_temp'] > 0)
        fingerprint = f"{desired}|{m['opens_real']}|{m['clicks_real']}" if writer.has_tracking_columns else desired
        if not pending and m['sheet_status'] == desired and (m['sync_note'] or '').startswith(fingerprint):
            continue
        tv = {'first_open_at': m['first_open'], 'open_count': m['opens_real'] or None,
              'click_count': m['clicks_real'] or None}
        r = writer.apply(m['tab'], m['row_no'], m['recipient'], desired, apply=apply, tracking_values=tv)
        r['desired'] = desired
        r['from_pending_evidence'] = bool(pending)
        results.append(r)
        if apply and r['result'] in ('WRITTEN', 'ALREADY_SET'):
            store.set_sheet_status(m['id'], desired, fingerprint)
        elif apply and r['result'] in ('SKIPPED_STATUS', 'CONFLICT'):
            store.set_sheet_status(m['id'], desired, f"{fingerprint} blocked:{r['result']}:{r['current']}")
    return results
