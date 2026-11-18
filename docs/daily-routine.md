# pyEmail command guide

Run every command from the project folder (`pyEmail`). In PowerShell or the prompt: `python run.py <command>`.
Anything marked **writes** changes the Google Sheet or a local file; everything else is read-only.

---

## 1. Your daily routine (start here)

| Step | Command | What it does |
|---|---|---|
| 1 | *(in the Google Sheet)* | Add new recipients as new rows. Leave `Merge status` blank. Check the addresses first (about 1 in 3 bounced before). |
| 2 | `python run.py daily` | Refreshes replies/bounces, shows the status dashboard, what is ready to send, and what needs your attention. **Sends nothing.** |
| 3 | *(fix what step 2 lists)* | Missing data, bad addresses, duplicate rows, unknown statuses. |
| 4 | `python run.py daily --send` | Same report, then asks how many: type `SEND <number>` (from 1 up to the number ready) and it sends exactly that many (cap 100 per run). The rest stay pending. |
| 5 | next morning: `python run.py daily` | Shows yesterday's new bounces and replies under "NEW since last run". |

`daily.cmd` (double-click, or `daily.cmd --send`) does the same as `python run.py daily`.

**The Google Sheet is your live dashboard.** The background refresh updates `Merge status` every 15 minutes
(PC on): red = bounced, dark green = replied, green = opened, grey = sent.

---

## 2. Every command

### Daily and sending

| Command | What it does |
|---|---|
| `python run.py daily` | Full report. Refresh, dashboard, plan, attention list. **Writes** statuses to the Sheet (replies/bounces/opens). Sends nothing. |
| `python run.py daily --send` | Same, then you choose the amount: type `SEND 2` to send 2, `SEND 5` to send 5, and so on (1 up to what is ready, cap 100). A number that is too big, `0`, or anything else cancels and sends nothing. It sends the first ones in tab and row order. |
| `python run.py daily --send --limit 20` | Lowers the most you are allowed to type to 20 this run. |
| `python run.py daily --send --yes` | Skip the question and send the maximum (everything ready, up to the cap). Only if you are sure. |
| `python run.py send` | Refresh, then send **every** ready row immediately. **No typed confirmation and no default cap.** Prefer `daily --send`. |
| `python run.py send --limit 10` | Same, but stop after 10 send attempts. |
| `python run.py send --only "01 - HR and Recruiter:58"` | Send just one Sheet row (`"TAB NAME:ROW NUMBER"`). |
| `python run.py preview` | Read-only list of every row and what would happen to it. |
| `python run.py preview --full 1` | Also print the full rendered email (subject, body, attachment) for the first row of each tab. |
| `python run.py preview --only "01 - HR and Recruiter:58"` | Preview one row. |

Every real send first takes the run lock, refreshes replies/bounces, and re-checks each recipient right before
sending. If any of that fails, **nothing is sent** (`NOT SENDING: ...`).

### Checking status

| Command | What it does |
|---|---|
| `python run.py track report` | Totals: sent, confirmed delivered, opens, clicks, bounces, replies, with rates. `n/a` where there is no evidence. |
| `python run.py track report --by tab` | Same, one line per tab. Also `--by campaign`, `--by day`, `--by recipient`. |
| `python run.py track report --tab "01 - HR and Recruiter"` | Only one tab. |
| `python run.py track report --campaign tracking-test` | Only one campaign (a tab's name, or `tracking-test`). |
| `python run.py track report --recipient someone@example.com` | One recipient. |
| `python run.py track report --since 2026-11-01 --until 2026-11-30` | Date range (UTC, `--until` is exclusive). |
| `python run.py track uncertain` | Lists sends where Gmail may or may not have sent the email. Check your Sent folder. |
| `python run.py tabs` | Shows each tab's name and header row (also a quick way to re-sign in to Google). |
| `python run.py validate` | Checks the config files exist and shows `DRY_RUN` / `ENABLE_EMAIL_SENDING`. |

### Replies, bounces and opens (Gmail -> local database -> Sheet)

| Command | What it does |
|---|---|
| `python run.py refresh` | What the 15-minute task runs: ingest new replies/bounces, then sync the Sheet. **Writes.** **Never sends.** |
| `python run.py track ingest` | Preview of what Gmail contains (replies, permanent/temporary bounces, out-of-office) and the Sheet changes it would cause. Writes nothing. |
| `python run.py track ingest --apply` | Records those events in the local database only. Sheet untouched. |
| `python run.py track sync` | Preview of status changes to write to the Sheet. |
| `python run.py track sync --apply` | **Writes** those status changes to the Sheet. Statuses never move backwards. |
| `python run.py track ingest --days 90` | Look back further than the default 45 days. |
| `python run.py replies` / `replies --apply` | Older, address-based reply check. Preview / writes `RESPONDED`. |
| `python run.py bounces` / `bounces --apply` | Older, address-based bounce check. Preview / writes `BOUNCED`. |

`refresh` and `daily` already do the ingest + sync for you, so you rarely need the `track ingest` / `track sync` pair.

### Tracking server and tests

| Command | What it does |
|---|---|
| `python run.py track init` | Creates/updates the local tracking database and imports older sends. Safe to repeat. |
| `python run.py track serve` | Starts the tracking server (open pixel). The `pyEmail-Tracker` task already does this at logon. |
| `python run.py track test-email --to you@example.com` | Preview of a tracked test email to your own address. Sends nothing. |
| `python run.py track test-email --to you@example.com --apply` | **Sends** that one test email, then check `track report --campaign tracking-test` after you open it. |
| `python run.py track resolve --key "TAB|email" --resolve-as sent` | After checking Sent: the uncertain send DID go out (stays blocked). Use `unsent` if it did not (frees it). |

### Sheet setup and maintenance

| Command | What it does |
|---|---|
| `python run.py colors` | Preview of the status color rules for the Merge status column. |
| `python run.py colors --apply` | **Writes** the color rules (safe to repeat, never duplicates). |
| `python run.py fix-headers` | One-time: adds the `Merge status` / `Your_Original_Subject_Line` headers to the Follow-up tab. Already done. |
| `python run.py report` | Placeholder that prints "0 sent, 0 pending". Not implemented; use `track report`. |

### Background tasks (Windows Task Scheduler)

Run these in PowerShell from the project folder. Without `-Install` they only print a plan.

| Command | What it does |
|---|---|
| `powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Status` | Is the 15-minute refresh healthy? Shows last result code (`0` = OK, `267009` = running now), next run, log path. |
| `powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Install` | Create/replace the refresh task. Option: `-IntervalMinutes 30`. |
| `powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Uninstall` | Remove the refresh task. |
| `powershell -ExecutionPolicy Bypass -File scripts\install_tracker_task.ps1 -Status` | Is the tracking server task registered, and is it answering locally? |
| `powershell -ExecutionPolicy Bypass -File scripts\install_tracker_task.ps1 -Install` / `-Uninstall` | Create / remove the tracking-server task. |
| `Start-ScheduledTask -TaskName pyEmail-Refresh` | Run the refresh once, right now. (Also `pyEmail-Tracker`.) |
| `Get-ScheduledTask -TaskName pyEmail-Refresh` | Show the task (State: Ready / Running). |

### Tailscale (the public address for open tracking)

Use the full path in a normal prompt: `"C:\Program Files\Tailscale\tailscale.exe" <command>`.

| Command | What it does |
|---|---|
| `tailscale funnel status` | Should say `Funnel on` and the proxy to `http://127.0.0.1:8787`. |
| `tailscale status` | Is Tailscale signed in and running? |
| `tailscale funnel --bg 8787` | Turn the public address back on. |
| `tailscale funnel --https=443 off` | Turn the public address off. |
| `tailscale up` | Sign in again (opens your browser). |

### Developer

| Command | What it does |
|---|---|
| `python -m pytest -q` | Run all tests (they never send email). One test, `test_env_defaults`, checks that `.env` has the safe defaults and fails while sending is live. That is expected. |

---

## 3. Options reference

| Option | Used with | Meaning |
|---|---|---|
| `--send` | `daily` | Send after the report (asks you to confirm). |
| `--limit N` | `daily`, `send` | Stop after N send attempts this run (`daily` default 100). |
| `--yes` | `daily --send` | Skip the typed confirmation. |
| `--only "TAB:ROW"` | `preview`, `send` | Process one Sheet row only. |
| `--full N` | `preview` | Print N full emails per tab. |
| `--apply` | `track ingest/sync`, `colors`, `replies`, `bounces`, `track test-email` | Actually write/send. Without it, commands only preview. |
| `--days N` | `daily`, `send`, `refresh`, `track ingest`, `replies`, `bounces` | How far back to look in Gmail (default 45). |
| `--by`, `--tab`, `--campaign`, `--recipient`, `--since`, `--until` | `track report` | Group and filter the report. |
| `--to ADDRESS` | `track test-email` | Where to send the test. |
| `--key`, `--resolve-as` | `track resolve` | Which uncertain send, and what actually happened. |

---

## 4. The `.env` switches

| Setting | Now | Meaning |
|---|---|---|
| `DRY_RUN` | `false` | `true` blocks all sending (preview only). |
| `ENABLE_EMAIL_SENDING` | `true` | `false` blocks all sending. |
| `TRACKING_ENABLED` | `true` | Emails carry the invisible open pixel (plain text + tracked HTML copy). |
| `TRACKING_CLICKS` | `false` | `true` would also route your LinkedIn link through the tracker. It breaks if your PC is off. Leave off. |
| `TRACKING_BASE_URL` | your `ts.net` address | Public address of the tracking server. |
| `TRACKING_OPEN_MIN_DELAY_SECONDS` | default 20 | Opens sooner than this after sending are ignored as scanners. 60 is stricter. |
| `TRACKING_RATE_LIMIT_PER_MIN` | 600 | Protects the tracking server from floods. |

**To lock sending again**: set `DRY_RUN=true` in `.env`.
**To send once while locked**, without editing `.env` (lasts only for that PowerShell window):

```powershell
$env:DRY_RUN='false'; $env:ENABLE_EMAIL_SENDING='true'; python run.py daily --send
```

---

## 5. Statuses and colors in `Merge status`

| Status | Color | Meaning |
|---|---|---|
| (blank) | none | Not sent yet: will be sent by the next send. |
| `EMAIL_SENT` | grey | Gmail accepted it. |
| `EMAIL_OPENED` | green | The open pixel (or your older tool) saw it opened. |
| `RESPONDED` | dark green | A real reply arrived. Never emailed again, in any tab. |
| `BOUNCED` | red | Permanent delivery failure. Never emailed again, in any tab. |
| `BOUNCE_TEMPORARY` | yellow | Temporary delivery problem. Decide yourself whether to retry (clear the cell). |
| `NO_RECIPIENT` | orange | No valid recipient. |

Statuses only move forward. Anything else you type (`UNSUBSCRIBED`, `DO_NOT_TRACK`, ...) is respected and blocks that address everywhere.

---

## 6. Where things are

| What | Where |
|---|---|
| Background refresh log | `logs\refresh.log` |
| Daily report log (counts only) | `logs\daily.log` |
| Record of every real send | `logs\tracking.json` and `data\tracking.db` |
| Email templates | `templates\*.txt` |
| Tab-to-template mapping | `tab_mapping.json` |

---

## 7. If something goes wrong

* **"REFRESH FAILED ... Nothing was sent"**: Gmail or the Sheet could not be read. Run `python run.py tabs` (it asks you to sign in to Google again if needed), then re-run.
* **"Another pyEmail job is running"**: the background refresh is mid-run. Wait a minute.
* **"UNCERTAIN SEND"**: Gmail may or may not have sent it. Check your Sent folder, then `track resolve`. Never re-send blindly.
* **A row shows `SKIP_*`**: the reason is in the name (`SKIP_MISSING_FIELDS`, `SKIP_INVALID_EMAIL`, `SKIP_REPLIED_ELSEWHERE`, ...). The "NEEDS YOUR ATTENTION" section tells you the fix.
* **Statuses stopped updating**: run the refresh `-Status` command above. If the result code is not `0`, read the last lines of `logs\refresh.log`.
* **"tracking address: NOT REACHABLE"** in the daily report: Tailscale is disconnected. With click tracking on, `daily --send` and `send` refuse to run until it answers (links would be dead). Open the Tailscale app and connect (or run `tailscale up`; as Administrator, `Restart-Service Tailscale`), then check `tailscale funnel status`. If it says "No serve config", run `tailscale funnel --bg 8787`. Or set `TRACKING_CLICKS=false` to send without click links.
* **No opens are being recorded**: check `tailscale funnel status`, `install_tracker_task.ps1 -Status`, and that the PC was on.
* **Google sign-in expired**: `python run.py tabs` signs you in again.
