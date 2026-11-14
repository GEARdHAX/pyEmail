"""Colour-code the `Merge status` column with Google Sheets conditional formatting.

Formatting only: no cell value is read or written by this module. One rule per status per tab,
scoped to the Merge status column (data rows only, header excluded). Re-running is safe: a status
that already has OUR rule on that column is skipped (or recoloured in place), so rules never stack up.
Pre-existing rules (for example whole-row colouring from a mail-merge add-on) are never touched.
"""
from services.outreach_service import _norm

# status -> background colour (hex). Statuses not listed here are left uncoloured.
# Red / green / darker green / orange match the whole-row rules the sheet already had.
STATUS_COLORS = {
    'BOUNCED': 'E06666',           # red
    'BOUNCE_TEMPORARY': 'FFE599',  # yellow
    'EMAIL_OPENED': '93C47D',      # green
    'RESPONDED': '6AA84F',         # darker green
    'EMAIL_SENT': 'D9D9D9',        # light grey
    'NO_RECIPIENT': 'FF9900',      # orange
}


def _rgb(hex_color):
    return {k: int(hex_color[i:i + 2], 16) / 255.0 for k, i in (('red', 0), ('green', 2), ('blue', 4))}


def _find_rule(rules, sheet_id, col, status):
    """(index, rule) of our rule for this status on exactly this one column, else None.

    The API omits zero-valued fields (sheetId 0, startColumnIndex 0), so a missing value means 0.
    """
    for i, r in enumerate(rules or []):
        cond = (r.get('booleanRule') or {}).get('condition') or {}
        values = [v.get('userEnteredValue', '') for v in cond.get('values', [])]
        if cond.get('type') == 'TEXT_EQ' and values and values[0].strip().upper() == status:
            for rng in r.get('ranges', []):
                if rng.get('sheetId', 0) == sheet_id and rng.get('startColumnIndex', 0) == col and \
                        rng.get('endColumnIndex') == col + 1:
                    return i, r
    return None


def _same_color(rule, color):
    have = ((rule.get('booleanRule') or {}).get('format') or {}).get('backgroundColor') or {}
    want = _rgb(color)
    return all(abs(have.get(k, 0.0) - want[k]) < 0.01 for k in want)


def plan_color_rules(service, spreadsheet_id, sheets, cfg):
    """Return (requests, report) for every mapped tab. Nothing is changed."""
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields='sheets(properties(sheetId,title),conditionalFormats)').execute()
    by_title = {s['properties']['title']: s for s in meta.get('sheets', [])}
    hrow = cfg['header_row']
    requests, report = [], []
    for tab in cfg['tabs']:
        sheet = by_title.get(tab)
        if not sheet:
            report.append({'tab': tab, 'status': None, 'result': 'TAB_NOT_FOUND'})
            continue
        sid = sheet['properties'].get('sheetId', 0)
        header = sheets.get_sheet_data(spreadsheet_id, f"{sheets.quote_tab(tab)}!{hrow}:{hrow}")
        header = header[0] if header else []
        want = _norm(cfg.get('merge_status_header', 'Merge status'))
        col = next((i for i, h in enumerate(header) if _norm(h) == want), None)
        if col is None:
            report.append({'tab': tab, 'status': None, 'result': 'NO_MERGE_STATUS_COLUMN'})
            continue
        for status, color in STATUS_COLORS.items():
            new_rule = {
                'ranges': [{'sheetId': sid, 'startRowIndex': hrow, 'startColumnIndex': col,
                            'endColumnIndex': col + 1}],
                'booleanRule': {'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': status}]},
                                'format': {'backgroundColor': _rgb(color)}}}
            found = _find_rule(sheet.get('conditionalFormats'), sid, col, status)
            if found and _same_color(found[1], color):
                report.append({'tab': tab, 'status': status, 'color': color, 'result': 'ALREADY_COLORED'})
            elif found:  # our rule exists with an older colour: update it in place, never duplicate
                requests.append({'updateConditionalFormatRule': {'index': found[0], 'sheetId': sid, 'rule': new_rule}})
                report.append({'tab': tab, 'status': status, 'color': color, 'result': 'WOULD_RECOLOR'})
            else:
                requests.append({'addConditionalFormatRule': {'index': 0, 'rule': new_rule}})
                report.append({'tab': tab, 'status': status, 'color': color, 'result': 'WOULD_ADD'})
    return requests, report


def apply_status_colors(service, spreadsheet_id, sheets, cfg, apply=False):
    requests, report = plan_color_rules(service, spreadsheet_id, sheets, cfg)
    if apply and requests:
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={'requests': requests}).execute()
        for r in report:
            if r['result'] == 'WOULD_ADD':
                r['result'] = 'ADDED'
            elif r['result'] == 'WOULD_RECOLOR':
                r['result'] = 'RECOLORED'
    return report
