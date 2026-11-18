"""Refresh = ingest the latest Gmail replies/bounces (apply locally) + sync status changes to the Sheet.

Two entry points, both holding the cross-process run lock so only one pyEmail job touches Gmail,
the Sheet and the tracking DB at a time:

* `send_campaign`  - refresh, then send. FAILS CLOSED: if the lock cannot be taken or the refresh
                     cannot complete for any reason, nothing is sent. Gmail sends themselves are never
                     retried (an ambiguous outcome is parked for a human, see OutreachService).
* `run_refresh_job`- the scheduled job. It has no sending code path at all: it never constructs an
                     OutreachService and never calls `send_email`. If the lock is busy it skips quietly.

Logs carry timestamps, counts and error CLASS names only: no addresses, no message text, no tokens.
"""
import logging
import re
import time
from collections import Counter
from logging.handlers import RotatingFileHandler

from services.event_ingest import ingest_all
from services.run_lock import RunBusy, RunLock
from services.status_sync import sync_statuses
from services.tracking_health import guard_before_send


class RefreshError(RuntimeError):
    """The refresh could not complete; callers must treat this as 'do not send'."""


# --- log hygiene ----------------------------------------------------------------------------------
EMAIL_LIKE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
TOKEN_LIKE = re.compile(r'[A-Za-z0-9_\-./+=]{24,}')


def sanitize(text, limit=160):
    """Strip addresses and long opaque strings (tokens, ids, URLs) from text bound for a log file."""
    text = EMAIL_LIKE.sub('<email>', str(text))
    text = TOKEN_LIKE.sub('<redacted>', text)
    text = ' '.join(text.split())
    return text[:limit]


_LOGGERS = {}


def get_refresh_logger(path):
    if path not in _LOGGERS:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        logger = logging.getLogger(f"pyemail.refresh.{abs(hash(path))}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        _LOGGERS[path] = logger
    return _LOGGERS[path]


# --- the refresh itself ---------------------------------------------------------------------------
def refresh(store, gmail, writer, settings, days=45):
    """Ingest (apply) then sync (apply). Raises RefreshError unless BOTH steps fully completed."""
    try:
        summary = ingest_all(store, gmail, days, apply=True)
    except Exception as e:
        raise RefreshError(f"ingest failed ({type(e).__name__})") from e
    thread_errors = summary['unresolved'].get('thread_error', 0)
    if thread_errors:  # some Gmail threads could not be read: replies/bounces may be missing
        raise RefreshError(f"ingest incomplete: {thread_errors} Gmail lookup(s) failed")
    try:
        results = sync_statuses(store, writer, settings, apply=True)
    except Exception as e:
        raise RefreshError(f"sheet sync failed ({type(e).__name__})") from e
    failed = sum(1 for r in results if r['result'] == 'WRITE_FAILED')
    if failed:
        raise RefreshError(f"sheet sync incomplete: {failed} write(s) failed")
    sync = Counter(r['result'] for r in results)
    fresh = [{'category': d['category'], 'tab': d['tab'], 'row': d['row'], 'to': d['to']}
             for d in summary['detections']
             if d['new'] and d['category'] in ('reply', 'bounce_permanent', 'bounce_temporary')]
    return {
        'new_detections': fresh,   # for on-screen reports only; never written to logs
        'events': dict(summary['counts']), 'new_events': dict(summary['new']),
        'unresolved': dict(summary['unresolved']), 'backfilled': summary['backfilled'],
        'sync': dict(sync), 'sheet_writes': sync.get('WRITTEN', 0),
    }


def _counts_line(info):
    ev, new = info['events'], info['new_events']
    parts = [f"reply={ev.get('reply', 0)}", f"bounce_perm={ev.get('bounce_permanent', 0)}",
             f"bounce_temp={ev.get('bounce_temporary', 0)}", f"ooo={ev.get('out_of_office', 0)}",
             f"automated={ev.get('automated', 0)}", f"new_events={sum(new.values())}",
             f"sheet_writes={info['sheet_writes']}", f"unresolved={sum(info['unresolved'].values())}"]
    return ' '.join(parts)


def _default_probe():
    from services.tracking_health import check_tracking_endpoint
    return check_tracking_endpoint


# --- guarded send ---------------------------------------------------------------------------------
def send_campaign(outreach, lock_path, log_path, days=45, lock_timeout=120):
    """Refresh, then send. Nothing is sent unless the lock is held AND the refresh fully completed."""
    if not (outreach.store and outreach.gmail and outreach.writer):
        raise RefreshError("refresh-before-send needs the tracking store, Gmail and the Sheet writer")
    log = get_refresh_logger(log_path)
    lock = RunLock(lock_path)
    if not lock.acquire(lock_timeout):
        log.error("stage=pre-send status=BLOCKED reason=lock_busy")
        raise RunBusy("another pyEmail job is running; not sending")
    started = time.monotonic()
    try:
        try:
            ok, detail = guard_before_send(outreach.track, getattr(outreach, 'tracking_probe', None) or _default_probe())
            if not ok:
                log.warning(f"stage=pre-send tracking_address=UNREACHABLE detail={detail} (opens-only: sending allowed)")
            info = refresh(outreach.store, outreach.gmail, outreach.writer, outreach.track, days)
        except Exception as e:
            log.error(f"stage=pre-send status=FAILED_CLOSED error={type(e).__name__}: {sanitize(e)}")
            raise
        log.info(f"stage=pre-send status=OK duration_s={time.monotonic() - started:.1f} {_counts_line(info)}")
        results = outreach.process_all()
        sent = Counter(r.get('status') for r in results)
        log.info(f"stage=send status=DONE sent={sent.get('SENT', 0)} failed={sent.get('SEND_FAILED', 0)} "
                 f"skipped={sum(v for k, v in sent.items() if k and k.startswith(('SKIP', 'FLAG')))}")
        return results
    finally:
        lock.release()


# --- the scheduled job (never sends) --------------------------------------------------------------
def run_refresh_job(build, lock_path, log_path, days=45, lock_timeout=0):
    """Run one refresh under the lock. Returns (exit_code, info). Skips (exit 0) if another job runs.

    `build` is a zero-argument callable returning (store, gmail, writer, settings); it is called only
    AFTER the lock is held and must not open a browser (use GmailService(..., interactive=False)).
    """
    log = get_refresh_logger(log_path)
    lock = RunLock(lock_path)
    if not lock.acquire(lock_timeout):
        holder = lock.holder()
        log.info(f"status=SKIPPED_LOCKED holder_pid={holder.get('pid')} since={holder.get('since')}")
        return 0, {'status': 'SKIPPED_LOCKED'}
    started = time.monotonic()
    try:
        store, gmail, writer, settings = build()
        info = refresh(store, gmail, writer, settings, days)
        log.info(f"status=OK duration_s={time.monotonic() - started:.1f} {_counts_line(info)}")
        return 0, {'status': 'OK', **info}
    except Exception as e:
        log.error(f"status=FAILED duration_s={time.monotonic() - started:.1f} "
                  f"error={type(e).__name__}: {sanitize(e)}")
        return 1, {'status': 'FAILED', 'error': type(e).__name__}
    finally:
        lock.release()
