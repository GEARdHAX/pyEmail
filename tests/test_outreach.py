"""Mocked end-to-end tests. Tab names/headers here are SYNTHETIC fixtures, not the real sheet."""
import base64
import email
import os
import tempfile
import unittest
from email import policy

from services.duplicate_prevention_service import DuplicatePreventionService
from services.email_template_service import EmailTemplateService
from services.gmail_service import GmailService
from services.google_sheets_service import GoogleSheetsService
from services.outreach_service import OutreachService
from services.tracking_service import TrackingService

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, 'templates')
_RESUME_DIR = tempfile.mkdtemp()
RESUME = os.path.join(_RESUME_DIR, 'resume.pdf')  # dummy file; tests never use the real resume
with open(RESUME, 'wb') as _f:
    _f.write(b'%PDF-1.4 test')

SUBJECTS = {
    'founder': "I'd like to help {{Company_Name}} as your next {{Job_Role}}",
    'cold': "A {{Job_Role}} who already understands what {{Company_Name}} does",
    'recruiter': "Application for {{Job_Role}} — Adarsh Arya, available immediately",
    'followup': "Re: {{Your_Original_Subject_Line}}",
}

K_FOUNDER = 'One_Specific_Thing_About_Their_Product_Or_Mission'
K_COLD = 'Something_Specific_A_Recent_Launch_Funding_News_Or_A_Gap_You_Could_Fill'
K_FOLLOW = 'One_New_Thing_A_Project_You_Shipped_A_Certification_Or_A_Relevant_Idea_For_Their_Company'
ACH = 'One_Concrete_Achievement_With_A_Number'


def sheet_data():
    # Four tabs with deliberately DIFFERENT headers and column orders.
    return {
        'T-Founders': [
            ['Merge status', 'Founders Name', 'Email', 'Company_Name', 'Job Role', 'Location', ACH, K_FOUNDER],
            ['', 'Fay', 'fay@f.com', 'FCo', 'Engineer', 'Delhi', 'cut costs 30%', 'their open API'],
            [],
            ['EMAIL_SENT', 'Sam', 'sam@f.com', 'SCo', 'Engineer', 'Delhi', 'a', 'b'],
            ['', 'NoMail', 'not-an-email', 'X', 'Engineer', 'Delhi', 'a', 'b'],
            ['', 'Gap', 'gap@f.com', 'GCo', '', 'Delhi', 'a', 'b'],
        ],
        'T-Cold': [
            ['Name', 'E-mail', 'Job_Role', 'Company_Name', 'Previous_Company', 'Location', ACH, K_COLD, 'Merge status'],
            ['Cy', 'cy@c.com', 'Analyst', 'CCo', 'PrevC', 'Pune', 'grew 2x', 'your Series A'],
        ],
        'T-Recruit': [
            ['Merge status', 'Recruiters_Name', 'Email', 'Company_Name', 'Job_Role', 'X_years', 'Key_Skill_1',
             'Key_Skill_2', 'Location', 'Previous_Company', ACH],
            ['', 'Rae', 'rae@r.com', 'RCo', 'Dev', '5', 'Python', 'SQL', 'Goa', 'PrevR', 'shipped 3 apps'],
        ],
        'T-Follow': [
            ['Name', 'Email', 'Company_Name', 'Job_Role', 'Your_Original_Subject_Line', K_FOLLOW, 'Merge status'],
            ['Fo', 'fo@x.com', 'FoCo', 'PM', 'A PM who already understands what FoCo does', 'I shipped X', ''],
        ],
    }


MAPPING = {
    'header_row': 1, 'merge_status_header': 'Merge status', 'sent_value': 'EMAIL_SENT',
    'tabs': {
        'T-Founders': {'template': 'founder', 'email_header': 'Email', 'attach_resume': True,
                       'field_map': {'Founders_Name': 'Founders Name'}},
        'T-Cold': {'template': 'cold', 'email_header': 'E-mail', 'attach_resume': True, 'field_map': {}},
        'T-Recruit': {'template': 'recruiter', 'email_header': 'Email', 'attach_resume': True, 'field_map': {}},
        'T-Follow': {'template': 'followup', 'email_header': 'Email', 'attach_resume': False, 'field_map': {}},
    },
}


class FakeSheets(GoogleSheetsService):
    def __init__(self, data, fail_updates=False):
        self.data, self.fail_updates, self.updates = data, fail_updates, []

    def list_tabs(self, sheet_id):
        return list(self.data)

    def get_tab_values(self, sheet_id, tab):
        return [list(r) for r in self.data[tab]]

    def update_status(self, sheet_id, range_name, status):
        if self.fail_updates:
            raise RuntimeError('sheets down')
        self.updates.append((range_name, status))


class FakeGmail:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def send_email(self, to, subject, body, attachment_path=None, subtype='html'):
        if to in self.fail_for:
            raise RuntimeError('gmail 500')
        self.sent.append(dict(to=to, subject=subject, body=body, attach=attachment_path, subtype=subtype))
        return {'id': f'm{len(self.sent)}'}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dup = DuplicatePreventionService(os.path.join(self.tmp, 'dup.json'))
        self.trk = TrackingService(os.path.join(self.tmp, 'stats.json'))

    def make(self, sheets, gmail, send=True):
        return OutreachService(sheets, gmail, EmailTemplateService(TPL), self.dup, self.trk,
                               MAPPING, 'SID', RESUME, send_enabled=send)


class TemplateTests(unittest.TestCase):
    def test_all_four_templates_load_with_exact_subjects(self):
        t = EmailTemplateService(TPL)
        for name, subj in SUBJECTS.items():
            self.assertEqual(t.load_template(f'{name}_subject', 'txt'), subj)
            self.assertTrue(t.load_template(name, 'txt').startswith('Hi {{'))

    def test_exact_body_fragments(self):
        t = EmailTemplateService(TPL)
        self.assertIn("I know you probably read these emails yourself, so I'll keep this short.",
                      t.load_template('founder', 'txt'))
        self.assertIn("Adarsh Arya · 7362973003 · https://www.linkedin.com/in/adarsharya2911/",
                      t.load_template('cold', 'txt'))
        fu = t.load_template('followup', 'txt')
        self.assertTrue(fu.endswith("Adarsh Arya · 7362973003"))  # no LinkedIn in follow-up, as supplied
        self.assertIn("I'll stop following up and cheer you on from the sidelines.", fu)

    def test_strict_render(self):
        r, m = EmailTemplateService.render_strict('Hi {{A}} {{B}}', {'A': 'x', 'B': ' '})
        self.assertIsNone(r)
        self.assertEqual(m, ['B'])
        r, m = EmailTemplateService.render_strict('Hi {{A}} {{A}}', {'A': 'x'})
        self.assertEqual(r, 'Hi x x')
        self.assertIsNone(m)
        r, m = EmailTemplateService.render_strict('Hi {{A}}', {'A': '{{B}}'})
        self.assertIsNone(r)


class PipelineTests(Base):
    def test_full_run_all_four_tabs(self):
        sheets, gmail = FakeSheets(sheet_data()), FakeGmail()
        self.make(sheets, gmail).process_all()
        by_to = {m['to']: m for m in gmail.sent}
        self.assertEqual(sorted(by_to), ['cy@c.com', 'fay@f.com', 'fo@x.com', 'rae@r.com'])
        self.assertEqual(by_to['fay@f.com']['subject'], "I'd like to help FCo as your next Engineer")
        self.assertIn('Hi Fay,', by_to['fay@f.com']['body'])
        self.assertIn('their open API', by_to['fay@f.com']['body'])
        self.assertEqual(by_to['cy@c.com']['subject'], 'A Analyst who already understands what CCo does')
        self.assertIn('at PrevC I grew 2x.', by_to['cy@c.com']['body'])
        self.assertEqual(by_to['rae@r.com']['subject'],
                         'Application for Dev — Adarsh Arya, available immediately')
        self.assertIn('about 8 months of hands-on internship experience in Python and SQL', by_to['rae@r.com']['body'])
        self.assertEqual(by_to['fo@x.com']['subject'], 'Re: A PM who already understands what FoCo does')
        for m in gmail.sent:
            self.assertNotIn('{{', m['subject'] + m['body'])
            self.assertEqual(m['subtype'], 'plain')
        self.assertNotIn('RCo', by_to['cy@c.com']['body'])
        self.assertEqual(by_to['fay@f.com']['attach'], RESUME)
        self.assertIsNone(by_to['fo@x.com']['attach'])

    def test_merge_status_updates_only_the_right_cells(self):
        sheets, gmail = FakeSheets(sheet_data()), FakeGmail()
        self.make(sheets, gmail).process_all()
        self.assertEqual(sorted(sheets.updates), sorted([
            ("'T-Founders'!A2", 'EMAIL_SENT'),
            ("'T-Cold'!I2", 'EMAIL_SENT'),
            ("'T-Recruit'!A2", 'EMAIL_SENT'),
            ("'T-Follow'!G2", 'EMAIL_SENT'),
        ]))

    def test_skips_blank_invalid_sent_and_missing(self):
        sheets, gmail = FakeSheets(sheet_data()), FakeGmail()
        res = self.make(sheets, gmail).process_all()
        st = {(r['tab'], r.get('row')): r['status'] for r in res}
        self.assertEqual(st[('T-Founders', 3)], 'SKIP_BLANK')
        self.assertEqual(st[('T-Founders', 4)], 'SKIP_ALREADY_SENT')
        self.assertEqual(st[('T-Founders', 5)], 'SKIP_INVALID_EMAIL')
        self.assertEqual(st[('T-Founders', 6)], 'SKIP_MISSING_FIELDS')
        self.assertNotIn('sam@f.com', [m['to'] for m in gmail.sent])
        self.assertEqual(len(gmail.sent), 4)

    def test_failed_send_not_marked(self):
        sheets, gmail = FakeSheets(sheet_data()), FakeGmail(fail_for={'fay@f.com'})
        res = self.make(sheets, gmail).process_all()
        self.assertNotIn(("'T-Founders'!A2", 'EMAIL_SENT'), sheets.updates)
        self.assertFalse(self.dup.is_sent('T-Founders|fay@f.com'))
        self.assertIn('SEND_FAILED', [r['status'] for r in res])

    def test_status_update_failure_reconciles_without_resend(self):
        sheets, gmail = FakeSheets(sheet_data(), fail_updates=True), FakeGmail()
        svc = self.make(sheets, gmail)
        res = svc.process_all()
        self.assertEqual(len(gmail.sent), 4)
        self.assertIn('SENT_STATUS_UPDATE_FAILED', [r['status'] for r in res])
        sheets.fail_updates = False
        res = svc.process_all()
        self.assertEqual(len(gmail.sent), 4, 'must not resend')
        self.assertEqual(len(sheets.updates), 4)
        self.assertEqual(sum(r['status'] == 'RECONCILED' for r in res), 4)

    def test_preview_sends_nothing(self):
        sheets, gmail = FakeSheets(sheet_data()), FakeGmail()
        res = self.make(sheets, gmail, send=False).process_all()
        self.assertEqual(gmail.sent, [])
        self.assertEqual(sheets.updates, [])
        self.assertEqual(sum(r['status'] == 'PREVIEW' for r in res), 4)

    def test_missing_header_or_tab(self):
        data = sheet_data()
        data['T-Cold'][0][0] = 'Nombre'
        gmail = FakeGmail()
        res = self.make(FakeSheets(data), gmail).process_all()
        self.assertIn('TAB_CONFIG_ERROR', [r['status'] for r in res])
        self.assertNotIn('cy@c.com', [m['to'] for m in gmail.sent])
        del data['T-Follow']
        res = self.make(FakeSheets(data), FakeGmail()).process_all()
        self.assertIn('TAB_NOT_FOUND', [r['status'] for r in res])


class FollowUpTabTests(Base):
    """Tab 04 layout: Merge status / Your_Original_Subject_Line may be missing or blank."""

    def _data(self):
        return {'T-Follow': [
            ['Email', 'Name', K_FOLLOW, 'Job_Role', 'Company_Name', 'Campaign status'],
            ['fo@x.com', 'Fo', 'I shipped X', 'PM', 'FoCo', 'keep me'],
        ]}

    def svc(self, sheets, gmail):
        cfg = {**MAPPING, 'tabs': {'T-Follow': MAPPING['tabs']['T-Follow']}}
        return OutreachService(sheets, gmail, EmailTemplateService(TPL), self.dup, self.trk,
                               cfg, 'SID', RESUME, send_enabled=True)

    def test_missing_columns_blocks_tab(self):
        gmail = FakeGmail()
        res = self.svc(FakeSheets(self._data()), gmail).process_all()
        self.assertEqual(res[0]['status'], 'TAB_CONFIG_ERROR')
        self.assertEqual(gmail.sent, [])

    def test_ensure_headers_writes_header_cells_only(self):
        sheets = FakeSheets(self._data())
        added = self.svc(sheets, FakeGmail()).ensure_headers('T-Follow', ['Merge status', 'Your_Original_Subject_Line'])
        self.assertEqual(added, ['Merge status', 'Your_Original_Subject_Line'])
        self.assertEqual(sheets.updates, [("'T-Follow'!G1", 'Merge status'),
                                          ("'T-Follow'!H1", 'Your_Original_Subject_Line')])
        self.assertEqual(self.svc(sheets, FakeGmail()).ensure_headers('T-Follow', ['Campaign status']), [])

    def test_blank_original_subject_is_skipped_not_guessed(self):
        data = self._data()
        data['T-Follow'][0] += ['Merge status', 'Your_Original_Subject_Line']
        data['T-Follow'][1] += ['', '']
        data['T-Follow'].append(['ok@x.com', 'Ok', 'I shipped Y', 'PM', 'OkCo', 'keep', '', 'Re-use me'])
        sheets, gmail = FakeSheets(data), FakeGmail()
        res = self.svc(sheets, gmail).process_all()
        self.assertEqual([r['status'] for r in res], ['SKIP_MISSING_FIELDS', 'SENT'])
        self.assertEqual(gmail.sent[0]['subject'], 'Re: Re-use me')
        self.assertEqual(sheets.updates, [("'T-Follow'!G3", 'EMAIL_SENT')])  # Campaign status (F) untouched


class StatusHandlingTests(Base):
    def test_each_status(self):
        rows = [['Merge status', 'Email', 'Name', 'Job_Role', 'Company_Name', 'Campaign status']]
        cases = [('EMAIL_SENT', 'a@x.com', 'SKIP_ALREADY_SENT'), ('email_opened', 'b@x.com', 'SKIP_ALREADY_OPENED'),
                 ('BOUNCED', 'c@x.com', 'FLAG_BOUNCED'), ('NO_RECIPIENT', '', 'SKIP_NO_RECIPIENT'),
                 ('NO_RECIPIENT', 'd@x.com', 'SKIP_NO_RECIPIENT'), ('WEIRD', 'e@x.com', 'SKIP_UNKNOWN_STATUS'),
                 ('', 'f@x.com', 'SENT'), ('', 'bad', 'SKIP_INVALID_EMAIL')]
        for st, em, _ in cases:
            rows.append([st, em, 'N', 'R', 'C', 'camp'])
        cfg = {**MAPPING, 'tabs': {'T': {'template': 'followup', 'email_header': 'Email', 'attach_resume': False,
                                         'field_map': {'Your_Original_Subject_Line': 'Company_Name',
                                                       K_FOLLOW: 'Campaign status'}}}}
        sheets, gmail = FakeSheets({'T': rows}), FakeGmail()
        res = OutreachService(sheets, gmail, EmailTemplateService(TPL), self.dup, self.trk, cfg, 'SID',
                              RESUME, send_enabled=True).process_all()
        self.assertEqual([r['status'] for r in res], [c[2] for c in cases])
        self.assertEqual([m['to'] for m in gmail.sent], ['f@x.com'])
        self.assertEqual(sheets.updates, [("'T'!A8", 'EMAIL_SENT')])  # existing statuses never rewritten
        self.assertEqual(self.dup.sent.keys(), {'T|f@x.com'})

    def test_reconcile_never_overwrites_bounced(self):
        self.dup.mark_sent('T|c@x.com', 'old')
        rows = [['Merge status', 'Email', 'Name', 'Job_Role', 'Company_Name', 'Campaign status'],
                ['BOUNCED', 'c@x.com', 'N', 'R', 'C', 'x']]
        cfg = {**MAPPING, 'tabs': {'T': {'template': 'followup', 'email_header': 'Email', 'attach_resume': False,
                                         'field_map': {'Your_Original_Subject_Line': 'Company_Name',
                                                       K_FOLLOW: 'Campaign status'}}}}
        sheets = FakeSheets({'T': rows})
        OutreachService(sheets, FakeGmail(), EmailTemplateService(TPL), self.dup, self.trk, cfg, 'SID',
                        RESUME, send_enabled=True).process_all()
        self.assertEqual(sheets.updates, [])


class SheetHelpers(unittest.TestCase):
    def test_a1(self):
        g = GoogleSheetsService
        self.assertEqual([g.column_letter(i) for i in (0, 25, 26, 27, 701, 702)],
                         ['A', 'Z', 'AA', 'AB', 'ZZ', 'AAA'])
        self.assertEqual(g.quote_tab("Bob's Tab"), "'Bob''s Tab'")


class GmailMime(unittest.TestCase):
    def _send(self, attach, subject):
        captured = {}

        class Exec:
            def execute(self):
                return {'id': 'z'}

        class Msgs:
            def send(self, userId, body):
                captured['raw'] = body['raw']
                return Exec()

        class Users:
            def messages(self):
                return Msgs()

        class Svc:
            def users(self):
                return Users()

        g = object.__new__(GmailService)
        g.service = Svc()
        g.send_email('a@b.com', subject, 'Hi — there', attach, subtype='plain')
        return email.message_from_bytes(base64.urlsafe_b64decode(captured['raw']), policy=policy.default)

    def test_attachment_and_utf8(self):
        m = self._send(RESUME, SUBJECTS['recruiter'])
        self.assertEqual(m['subject'], SUBJECTS['recruiter'])
        names = [p.get_filename() for p in m.walk() if p.get_filename()]
        self.assertEqual(names, ['resume.pdf'])
        self.assertEqual(m.get_body(('plain',)).get_content().strip(), 'Hi — there')
        self.assertEqual(self._send(None, 'x').get_body(('plain',)).get_content().strip(), 'Hi — there')

    def test_missing_attachment_raises(self):
        with self.assertRaises(FileNotFoundError):
            self._send(os.path.join(TPL, 'nope.pdf'), 'x')


if __name__ == '__main__':
    unittest.main()
