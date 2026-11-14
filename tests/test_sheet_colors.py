"""Mocked tests for status colouring. A fake Sheets API object records what would be sent."""
import unittest

from services.google_sheets_service import GoogleSheetsService
from services.sheet_colors import STATUS_COLORS, _rgb, apply_status_colors, plan_color_rules

CFG = {'header_row': 1, 'merge_status_header': 'Merge status',
       'tabs': {'T1': {'email_header': 'Email'}, 'T2': {'email_header': 'Email'}, 'Gone': {'email_header': 'Email'},
                'NoStatus': {'email_header': 'Email'}}}


class FakeApi:
    def __init__(self, existing=None):
        self.existing, self.batches = existing or {}, []

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, fields):
        api = self

        class Req:
            def execute(self):
                return {'sheets': [
                    {'properties': {'sheetId': 11, 'title': 'T1'}, 'conditionalFormats': api.existing.get('T1', [])},
                    {'properties': {'sheetId': 22, 'title': 'T2'}},
                    {'properties': {'sheetId': 33, 'title': 'NoStatus'}}]}
        return Req()

    def batchUpdate(self, spreadsheetId, body):
        api = self

        class Req:
            def execute(self):
                api.batches.append(body)
                return {}
        return Req()


class FakeSheets(GoogleSheetsService):
    HEADERS = {'T1': ['Email', 'Name', 'Merge status', 'Campaign status'],
               'T2': ['Merge status', 'Email'], 'NoStatus': ['Email', 'Name']}

    def __init__(self):
        self.reads = []

    def get_sheet_data(self, sheet_id, range_name):
        self.reads.append(range_name)
        tab = range_name.split("'")[1]
        return [self.HEADERS[tab]]


def rule_for(status, sid, col, color=None, omit_zero=False):
    rng = {'sheetId': sid, 'startRowIndex': 1, 'startColumnIndex': col, 'endColumnIndex': col + 1}
    if omit_zero:  # the real API leaves out fields whose value is 0
        rng = {k: v for k, v in rng.items() if v != 0}
    return {'ranges': [rng],
            'booleanRule': {'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': status}]},
                            'format': {'backgroundColor': _rgb(color or STATUS_COLORS[status])}}}


class ColorTests(unittest.TestCase):
    def test_palette_matches_the_request(self):
        self.assertEqual(STATUS_COLORS['BOUNCED'], 'E06666')       # red
        self.assertEqual(STATUS_COLORS['EMAIL_OPENED'], '93C47D')  # green
        self.assertEqual(STATUS_COLORS['RESPONDED'], '6AA84F')     # darker green, as the sheet already used
        self.assertEqual(len(set(STATUS_COLORS.values())), len(STATUS_COLORS))  # every status distinct

    def test_one_rule_per_status_per_usable_tab(self):
        reqs, _ = plan_color_rules(FakeApi(), 'SID', FakeSheets(), CFG)
        self.assertEqual(len(reqs), 2 * len(STATUS_COLORS))  # T1 and T2 only: Gone / NoStatus are skipped

    def test_columns_rows_and_missing_tabs(self):
        reqs, report = plan_color_rules(FakeApi(), 'SID', FakeSheets(), CFG)
        cols = {}
        for r in reqs:
            rng = r['addConditionalFormatRule']['rule']['ranges'][0]
            cols.setdefault(rng['sheetId'], set()).add((rng['startColumnIndex'], rng['endColumnIndex'], rng['startRowIndex']))
        self.assertEqual(cols, {11: {(2, 3, 1)}, 22: {(0, 1, 1)}})  # status column only, header row excluded
        results = {(r['tab'], r['result']) for r in report if r['result'] != 'WOULD_ADD'}
        self.assertEqual(results, {('Gone', 'TAB_NOT_FOUND'), ('NoStatus', 'NO_MERGE_STATUS_COLUMN')})

    def test_rule_content(self):
        reqs, _ = plan_color_rules(FakeApi(), 'SID', FakeSheets(), CFG)

        def colour(status):
            r = next(x for x in reqs if x['addConditionalFormatRule']['rule']['ranges'][0]['sheetId'] == 11 and
                     x['addConditionalFormatRule']['rule']['booleanRule']['condition']['values'][0]['userEnteredValue'] == status)
            return r['addConditionalFormatRule']['rule']['booleanRule']['format']['backgroundColor']
        red, green = colour('BOUNCED'), colour('EMAIL_OPENED')
        self.assertTrue(red['red'] > red['green'] and red['red'] > red['blue'])
        self.assertTrue(green['green'] > green['red'] and green['green'] > green['blue'])

    def test_preview_sends_nothing(self):
        api = FakeApi()
        report = apply_status_colors(api, 'SID', FakeSheets(), CFG, apply=False)
        self.assertEqual(api.batches, [])
        self.assertTrue(any(r['result'] == 'WOULD_ADD' for r in report))

    def test_apply_is_a_single_batch_of_formatting_requests_only(self):
        api = FakeApi()
        report = apply_status_colors(api, 'SID', FakeSheets(), CFG, apply=True)
        self.assertEqual(len(api.batches), 1)
        kinds = {next(iter(r)) for r in api.batches[0]['requests']}
        self.assertEqual(kinds, {'addConditionalFormatRule'})  # never updateCells / values
        self.assertEqual({r['result'] for r in report if r['status']}, {'ADDED'})

    def test_rerun_does_not_stack_duplicate_rules(self):
        api = FakeApi({'T1': [rule_for(s, 11, 2) for s in STATUS_COLORS]})
        report = apply_status_colors(api, 'SID', FakeSheets(), CFG, apply=True)
        t1 = [r for r in report if r['tab'] == 'T1' and r['status']]
        self.assertEqual({r['result'] for r in t1}, {'ALREADY_COLORED'})
        sent = [q['addConditionalFormatRule']['rule']['ranges'][0]['sheetId'] for q in api.batches[0]['requests']]
        self.assertNotIn(11, sent)
        self.assertIn(22, sent)

    def test_nothing_to_do_means_no_api_write(self):
        cfg = {**CFG, 'tabs': {'T1': CFG['tabs']['T1']}}
        api = FakeApi({'T1': [rule_for(s, 11, 2) for s in STATUS_COLORS]})
        apply_status_colors(api, 'SID', FakeSheets(), cfg, apply=True)
        self.assertEqual(api.batches, [])

    def test_pre_existing_row_rules_are_left_alone_and_not_mistaken_for_ours(self):
        wide = rule_for('BOUNCED', 11, 0)
        wide['ranges'][0].update(startColumnIndex=0, endColumnIndex=12)   # a whole-row rule from an add-on
        api = FakeApi({'T1': [wide]})
        cfg = {**CFG, 'tabs': {'T1': CFG['tabs']['T1']}}
        report = apply_status_colors(api, 'SID', FakeSheets(), cfg, apply=True)
        self.assertEqual({r['status']: r['result'] for r in report}['BOUNCED'], 'ADDED')   # ours is still added
        self.assertNotIn('updateConditionalFormatRule', {next(iter(r)) for r in api.batches[0]['requests']})

    def test_sheet_id_zero_and_column_zero_are_recognised_despite_being_omitted_by_the_api(self):
        class Api(FakeApi):
            def get(self_inner, spreadsheetId, fields):
                class Req:
                    def execute(_):
                        return {'sheets': [{'properties': {'title': 'T2'},   # sheetId 0 is omitted by the API
                                            'conditionalFormats': [rule_for(s, 0, 0, omit_zero=True)
                                                                   for s in STATUS_COLORS]}]}
                return Req()
        cfg = {**CFG, 'tabs': {'T2': CFG['tabs']['T2']}}
        api = Api()
        report = apply_status_colors(api, 'SID', FakeSheets(), cfg, apply=True)
        self.assertEqual({r['result'] for r in report}, {'ALREADY_COLORED'})
        self.assertEqual(api.batches, [])  # nothing duplicated

    def test_old_colour_is_updated_in_place_not_duplicated(self):
        api = FakeApi({'T1': [rule_for(s, 11, 2, color='4285F4' if s == 'RESPONDED' else None) for s in STATUS_COLORS]})
        cfg = {**CFG, 'tabs': {'T1': CFG['tabs']['T1']}}
        report = apply_status_colors(api, 'SID', FakeSheets(), cfg, apply=True)
        self.assertEqual({r['status']: r['result'] for r in report if r['result'] != 'ALREADY_COLORED'},
                         {'RESPONDED': 'RECOLORED'})
        (req,) = api.batches[0]['requests']
        self.assertEqual(list(req), ['updateConditionalFormatRule'])
        self.assertEqual(req['updateConditionalFormatRule']['index'], list(STATUS_COLORS).index('RESPONDED'))


if __name__ == '__main__':
    unittest.main()
