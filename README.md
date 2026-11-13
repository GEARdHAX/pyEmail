# Python Gmail Outreach Automation

A modular Python system to automate personalized email outreach from Google Sheets via Gmail API.

## Setup
1. Create a Google Cloud Project and enable Gmail API and Google Sheets API.
2. Download OAuth 2.0 `credentials.json`.
3. Set up `.env` with:
   ```env
   GOOGLE_SHEET_ID=your_sheet_id
   GOOGLE_CREDENTIALS_FILE=credentials.json
   GMAIL_TOKEN_FILE=token.json
   RESUME_PATH=assets/resume.pdf
   DRY_RUN=true
   ENABLE_EMAIL_SENDING=false
   ```
4. Run `python main.py validate` to check configuration.

## Commands
- `python main.py preview`: Check outreach details without sending.
- `python main.py send`: Execute outreach and update status.
- `python main.py report`: Summarize results.
- `python main.py validate`: Verify setup.
