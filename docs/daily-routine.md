# Daily routine

One command does the whole job: `python run.py daily` (or double-click `daily.cmd`).

## Every day

| Step | Command | What it does |
|---|---|---|
| 1 | *(in the Google Sheet)* | Add new recipients as new rows. Leave `Merge status` blank. Check the addresses first (about 1 in 3 bounced before). |
| 2 | `python run.py daily` | Refreshes replies/bounces, shows the status dashboard, what is ready to send, and what needs your attention. **Sends nothing.** |
| 3 | fix anything under "NEEDS YOUR ATTENTION" | Missing data, bad addresses, duplicate rows, unknown statuses. |
| 4 | `python run.py daily --send` | Same report, then asks you to type `SEND <n>` and sends at most 100 emails. |
| 5 | next morning: `python run.py daily` | Shows yesterday's new bounces and replies ("NEW since last run"). |

Sending is locked in `.env` (`DRY_RUN=true`, `ENABLE_EMAIL_SENDING=false`). To send **once** without editing `.env`:

```powershell
$env:DRY_RUN='false'; $env:ENABLE_EMAIL_SENDING='true'; python run.py daily --send
```

(The setting only lasts for that PowerShell window. Close it afterwards.)

## Options

| Option | Meaning |
|---|---|
| `--send` | send after the report (needs the unlock above and your typed confirmation) |
| `--limit N` | send at most N emails this run (default 100); the rest wait for the next run |
| `--yes` | skip the typed confirmation (only if you are sure) |

## Occasionally

| Command | When |
|---|---|
| `python run.py track report --by tab` | opens/bounces/replies statistics (opens only count when tracking is on) |
| `python run.py track uncertain` | if the daily report lists an "UNCERTAIN SEND": check Gmail Sent, then `track resolve --key "TAB\|email" --resolve-as sent` (or `unsent`) |
| `python run.py colors --apply` | re-apply the status colours if a rule was deleted (safe to repeat) |
| `python run.py preview` | read-only look at every row without the report |
| `powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Status` | check the 15-minute background refresh |

## If something goes wrong

* **"REFRESH FAILED ... Nothing was sent"**: Gmail or the Sheet could not be read. Run `python run.py tabs` once (it will ask you to sign in to Google again if needed), then re-run `daily`.
* **"Another pyEmail job is running"**: the background refresh is mid-run. Wait a minute.
* **"UNCERTAIN SEND"**: Gmail may or may not have sent that email. Never re-send blindly: check your Sent folder first.
* **A row shows `SKIP_*` in the report**: the reason is in the name (`SKIP_MISSING_FIELDS`, `SKIP_INVALID_EMAIL`, ...). The "NEEDS YOUR ATTENTION" section tells you the fix.
