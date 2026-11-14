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

    def list_tabs(self, sheet_id):
        """Exact tab titles, in workbook order."""
        meta = self.service.spreadsheets().get(
            spreadsheetId=sheet_id, fields='sheets.properties.title').execute()
        return [s['properties']['title'] for s in meta.get('sheets', [])]

    @staticmethod
    def quote_tab(tab):
        return "'" + tab.replace("'", "''") + "'"

    @staticmethod
    def column_letter(index):
        """0-based column index -> A1 letters."""
        letters = ''
        index += 1
        while index:
            index, rem = divmod(index - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    def get_tab_values(self, sheet_id, tab):
        """All values of a tab; row i of the result is sheet row i+1."""
        return self.get_sheet_data(sheet_id, self.quote_tab(tab))

    def update_cell(self, sheet_id, tab, column_index, row_number, value):
        """Write exactly one cell (0-based column, 1-based row)."""
        cell = f"{self.quote_tab(tab)}!{self.column_letter(column_index)}{row_number}"
        self.update_status(sheet_id, cell, value)
        return cell
