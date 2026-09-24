"""The progress panel renders committed counters and diagnostics as text."""
from pathlib import Path
import re
import unittest
from playwright.sync_api import sync_playwright


class Reh2ProgressTests(unittest.TestCase):
    def test_invalid_package_is_not_displayed_as_ready(self):
        html = (Path(__file__).resolve().parents[1] / 'medik_pilot/web/index.html').read_text()
        scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('<p id="reason"></p>')
                for source in scripts:
                    page.evaluate('source => { new Function(source); }', source)
                page.evaluate('window.reasonLabels = {}')
                page.add_script_tag(content=next(s for s in scripts if 'function renderTestSource' in s))
                page.evaluate('run => renderTestSource(run)', {
                    'test_source': 'reh2', 'reference_tests': 3500,
                    'collected_by_kind': {'test': 0},
                    'test_progress': {'phase': 'committed', 'processed': 80, 'package_count': 80,
                        'received': 240, 'ready': 0, 'existing': 0, 'invalid': 240,
                        'diagnostics': [{'count': 240, 'reason': 'Неоднозначное совпадение <b>тест</b>'}]}})
                text = page.locator('#testSourceProgress').inner_text()
                self.assertIn('готовых уникальных для запуска: 0', text)
                self.assertIn('готовых записей: 0', text)
                self.assertIn('некорректных: 240', text)
                self.assertIn('Неоднозначное совпадение <b>тест</b>', text)
                self.assertEqual(page.locator('#testSourceProgress b').count(), 0)
            finally:
                browser.close()
