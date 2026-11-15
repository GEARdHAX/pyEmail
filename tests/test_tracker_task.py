"""Static + plan-mode checks for the tracking-server startup task script (registers nothing)."""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'install_tracker_task.ps1')


class TrackerTaskScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SCRIPT, encoding='utf-8') as f:
            cls.text = f.read()
        code = re.sub(r'<#.*?#>', '', cls.text, flags=re.S)
        cls.code = re.sub(r'(?m)^\s*#.*$', '', code)

    def test_it_runs_only_the_tracking_server(self):
        self.assertRegex(self.code, r"\$arguments\s*=\s*'run\.py track serve'")
        self.assertEqual(re.findall(r"-Argument\s+(\S+)", self.code), ['$arguments'])
        for forbidden in ('run.py send', 'run.py daily', 'run.py refresh'):
            self.assertNotIn(forbidden, self.code)

    def test_starts_at_logon_restarts_on_crash_and_never_doubles_up(self):
        self.assertIn('-AtLogOn', self.code)
        self.assertIn('-MultipleInstances IgnoreNew', self.code)
        self.assertRegex(self.code, r'-RestartCount\s+5')
        self.assertIn('[TimeSpan]::Zero', self.code)             # a server must not be killed by a time limit

    def test_registration_needs_an_explicit_install_switch(self):
        before = self.code.split('Register-ScheduledTask -TaskName')[0]
        self.assertIn('if (-not $Install)', before)
        self.assertEqual(self.code.count('Register-ScheduledTask -TaskName'), 1)

    def test_no_secrets_or_passwords(self):
        self.assertNotRegex(self.text, r'(?i)-Password|client_secret|refresh_token|token\.json|\.env')

    def test_server_is_bound_to_localhost_only(self):
        from services.tracking_settings import load_settings
        self.assertEqual(load_settings({}).host, '127.0.0.1')
        self.assertIn('127.0.0.1:8787 only', self.text)

    @unittest.skipUnless(shutil.which('powershell'), 'PowerShell not available')
    def test_plan_mode_runs_and_registers_nothing(self):
        r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT,
                            '-TaskName', 'pyEmail-Tracker-UnitTest'], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('run.py track serve', r.stdout)
        self.assertIn('Nothing was registered', r.stdout)
        chk = subprocess.run(['powershell', '-NoProfile', '-Command',
                              "(Get-ScheduledTask -TaskName 'pyEmail-Tracker-UnitTest' -ErrorAction SilentlyContinue | "
                              "Measure-Object).Count"], capture_output=True, text=True, timeout=60)
        self.assertEqual(chk.stdout.strip(), '0')


if __name__ == '__main__':
    unittest.main()
