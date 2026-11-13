from googleapiclient.discovery import build
from services.logger import get_logger

logger = get_logger(__name__)

class GoogleSheetsService:
    def __init__(self, service_account_creds):
        self.service = build('sheets', 'v4', credentials=service_account_creds)

    def get_sheet_data(self, sheet_id, range_name):
        result = self.service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=range_name).execute()
        return result.get('values', [])

    def update_status(self, sheet_id, range_name, status):
        body = {'values': [[status]]}
        self.service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=range_name,
            valueInputOption='RAW', body=body).execute()
