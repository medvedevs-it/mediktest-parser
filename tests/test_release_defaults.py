import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ReleaseDefaultsTests(unittest.TestCase):
    def test_new_api_runs_default_to_reh2_and_allow_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, MEDIKTEST_DATA_DIR=directory)
            subprocess.run([sys.executable, '-c',
                "from medik_pilot.app import RunRequest; "
                "assert RunRequest().test_source == 'reh2'; "
                "assert RunRequest(test_source='legacy').test_source == 'legacy'"],
                env=env, check=True, capture_output=True, timeout=30)

    def test_new_form_selects_reh2(self):
        html = (Path(__file__).resolve().parents[1]/'medik_pilot/web/index.html').read_text()
        self.assertIn('<option value="reh2" selected>', html)
        self.assertIn('<option value="legacy">', html)
