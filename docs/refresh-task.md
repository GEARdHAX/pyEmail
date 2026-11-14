# Scheduled refresh and refresh-before-send

## What each piece does

| Command | Reads Gmail | Writes Sheet | Sends email |
|---|---|---|---|
| `python run.py refresh` (scheduled, every 15 min) | replies + bounces | `Merge status` only (BOUNCED, RESPONDED, ...) | **never** |
| `python run.py send` | same refresh first | `Merge status` | only after the refresh fully succeeds, and only if `DRY_RUN=false` and `ENABLE_EMAIL_SENDING=true` |

`refresh` ignores `DRY_RUN` / `ENABLE_EMAIL_SENDING` because it has no sending code path at all.

## Refresh-before-send (`run.py send`)

1. Safety locks are checked first (unchanged). If sending is disabled, nothing else runs.
2. The run lock is taken (waits up to 120 s for a running refresh; otherwise refuses).
3. **Refresh:** ingest new replies/bounces into the local tracking DB, then sync status changes to the Sheet.
   Any failure (Gmail error, unreadable thread, Sheet read/write error, expired Google sign-in) aborts the
   send with `NOT SENDING: ...`. Nothing is sent and nothing is reserved.
4. Each recipient is **re-checked immediately before its email**: the live Sheet row (same address, status
   still blank) and the tracking DB (no reply/permanent bounce recorded). Any doubt means skip.
5. Suppressed addresses (replied, permanently bounced, `UNSUBSCRIBED`, `DO_NOT_TRACK`, `NO_RECIPIENT`, ...)
   are skipped in **every** tab. A temporary bounce only flags its own row.
6. Gmail sends are never retried automatically. A timeout or 5xx parks the row as `uncertain` for a human
   (`run.py track uncertain` / `track resolve`).

## Windows Task Scheduler

```powershell
# show the plan only (registers nothing)
powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1
# create / replace the task (every 15 minutes, current user, only while logged on)
powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Install
# check last/next run
powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Status
# remove it
powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Uninstall
```

Overlap protection has two layers: Task Scheduler `MultipleInstances = IgnoreNew`, and an OS file lock
(`data\pyemail.lock`) shared by `refresh` and `send`. The lock is released by the OS if a process crashes,
so it can never get stuck.

Logs: `logs\refresh.log` (rotating). Lines hold a timestamp, `status=OK|FAILED|SKIPPED_LOCKED`, counts and
the error class only: no addresses, no message text, no tokens.

If Google sign-in expires the task fails with `AuthRequired` (it never opens a browser). Fix it once by
running any interactive command, e.g. `python run.py tabs`.
