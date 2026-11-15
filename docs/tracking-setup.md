# Open tracking setup (Tailscale Funnel)

Open tracking adds a tiny invisible image to each email. When the recipient's mail app loads it, the
tracking server records the open. pyEmail's emails then go out as plain text (exactly your template) **plus**
an HTML copy that carries the image. Click tracking is a separate switch and is off.

## How the pieces fit

| Piece | What it is | Starts |
|---|---|---|
| Tailscale Funnel | publishes `127.0.0.1:8787` at `https://<this-pc>.<your-tailnet>.ts.net` | by itself (Tailscale service) |
| Tracking server | `python run.py track serve` (no Google access, never sends) | at logon, via `scripts\install_tracker_task.ps1 -Install` |
| Refresh task | `python run.py refresh` every 15 min | already installed |

Tracking only works while this PC is on and you are logged in. If it is off when someone opens an email,
that open is simply not recorded; nothing visible breaks (the recipient just sees no image).

## One-time setup (all steps done on this PC)

1. Tailscale installed and signed in; Funnel enabled on the account. **Done.**
2. `tailscale funnel --bg 8787`. **Done** (check: `tailscale funnel status`).
3. `.env`: `TRACKING_BASE_URL=https://<this-pc>.<your-tailnet>.ts.net`, `TRACKING_RATE_LIMIT_PER_MIN=600`. **Done.**
4. Start the server automatically at logon:
   `powershell -ExecutionPolicy Bypass -File scripts\install_tracker_task.ps1 -Install`
5. Turn tracking on: set `TRACKING_ENABLED=true` in `.env` (keep `TRACKING_CLICKS=false` for opens only). **Done.**

## Check it

```
powershell -ExecutionPolicy Bypass -File scripts\install_tracker_task.ps1 -Status
python run.py track report --by tab          # opens appear here once tracked emails are sent
```

## Limits to expect

* Gmail loads images through its own proxy, so you see **one open per email** at best, and the first "open"
  within about 20 seconds of sending is flagged as a scanner and not counted.
* Apple Mail pre-loads images for its users, which can show an open that no human made.
* Recipients who block images are never counted.
* Tracked emails are HTML, which can make spam filters slightly more suspicious of cold emails.

## Turn it off

`TRACKING_ENABLED=false` in `.env` (emails go back to plain text only); optionally
`scripts\install_tracker_task.ps1 -Uninstall` and `tailscale funnel --https=443 off`.
