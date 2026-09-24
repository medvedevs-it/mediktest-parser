"""Offline browser regression checks against the observed case-page structure."""
import ast
import inspect
import textwrap
import unittest

from playwright.sync_api import sync_playwright
from medik_pilot.collectors.live import LiveSelftestCollector
from medik_pilot.config import Settings


class CaseDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.collector = LiveSelftestCollector(Settings.from_env())
        self.collector._page = self.page
        self.page.set_content('''
          <div>Диагноз<p>Совпадающий ответ</p></div>
          <div id="panel"><nav aria-label="Список вопросов"><ul>
            <li><a class="page-link" href="#" onclick="show(1)">1</a></li>
            <li class="active"><a class="page-link" href="#" onclick="show(12)">12</a></li>
          </ul></nav><h5 class="adoc">Последний вопрос</h5>
          <div class="text-success"><input class="custom-control-input" type="radio" disabled>
          <label class="custom-control-label">Совпадающий ответ</label></div>
          <div><label class="custom-control-label">10<sup>9</sup>/л</label></div></div>
          <div>1</div>
          <script>function show(n) {setTimeout(()=>{
            document.querySelectorAll('li').forEach(li=>li.classList.toggle('active',li.textContent==n));
            document.querySelector('h5').innerHTML=n==1?'Первый вопрос с 10<sup>9</sup>/л':'Последний вопрос';
          },200);}</script>''')

    def tearDown(self):
        self.page.close()

    def test_explicit_pagination_waits_for_first_question(self):
        self.collector._open_case_question(1)
        q = self.collector._read_case_question(1)
        self.assertEqual(q['question'], 'Первый вопрос с 10⁹/л')
        self.assertEqual(q['options'][1]['text'], '10⁹/л')
        self.assertTrue(q['options'][0]['is_correct'])

    def test_condition_option_match_does_not_replace_question(self):
        self.assertEqual(self.collector._read_case_question(12)['question'], 'Последний вопрос')

    def test_wrong_active_number_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Номер'):
            self.collector._read_case_question(1)

    def test_completed_result_popup_is_closed_before_reading_first_question(self):
        for result in ('0 вопросов', '1 вопрос', '2 вопроса', '5 вопросов', '12 вопросов'):
            with self.subTest(result=result):
                self.page.evaluate('''result => {
                  const d=document.createElement('div'); d.setAttribute('role','dialog');
                  d.innerHTML='Результаты решения задачи <button onclick="this.parentElement.remove()">×</button> Вы ответили верно на '+result+' из 12.';
                  document.body.appendChild(d);
                }''', result)
                self.collector._open_case_question(1)
                self.assertEqual(self.collector._read_case_question(1)['question'], 'Первый вопрос с 10⁹/л')

    def test_equal_count_validation_dialog_is_closed_and_retried_once(self):
        self.page.set_content('''<input class="custom-control-input" type="checkbox" checked>
          <button id="next" onclick="window.retries=(window.retries||0)+1">Далее</button>
          <div role="dialog">Неверное количество вариантов ответа
          <button onclick="this.parentElement.style.display='none'">×</button>
          В текущем вопросе необходимо выбрать 1 вариантов ответа. Выбрано вариантов ответа: 1.</div>''')
        self.collector._check_case_answer_dialog(1, self.page.locator('#next'))
        self.assertEqual(self.page.evaluate('window.retries'), 1)

    def test_unknown_dialog_is_not_dismissed(self):
        self.page.set_content('<button id="next">Далее</button><div role="dialog">Доступ ограничен<button>×</button></div>')
        with self.assertRaisesRegex(RuntimeError, 'отклонил'):
            self.collector._check_case_answer_dialog(1, self.page.locator('#next'))
        self.assertTrue(self.page.locator('[role="dialog"]').is_visible())

    def test_new_results_popup_is_acknowledged_without_resubmission(self):
        self.page.set_content('''<button id="next" onclick="window.retries=1">Далее</button>
          <div role="dialog">Доступны новые данные
          <button onclick="this.parentElement.style.display='none'">×</button>
          Результаты лабораторных методов обследования</div>''')
        self.collector._check_case_answer_dialog(1, self.page.locator('#next'))
        self.assertFalse(self.page.locator('[role="dialog"]').is_visible())
        self.assertIsNone(self.page.evaluate('window.retries'))

    def test_unequal_answer_counts_are_not_retried(self):
        self.page.set_content('''<button id="next">Далее</button><div role="dialog">
          Неверное количество вариантов ответа<button>×</button>
          В текущем вопросе необходимо выбрать 5 вариантов ответа. Выбрано вариантов ответа: 4.</div>''')
        with self.assertRaisesRegex(RuntimeError, 'отклонил'):
            self.collector._check_case_answer_dialog(5, self.page.locator('#next'))

    def test_sections_preserve_superscripts_in_tables_and_text(self):
        self.page.set_content('''<a class="nav-link" href="#i1">Анализ</a>
          <div id="i1"><p>Лейкоциты 10<sup>9</sup>/л</p>
          <table><tr><td>Эритроциты</td><td>10<sup>12</sup>/л</td></tr></table>
          <p>H<sub>2</sub>O</p></div>''')
        # Exercise the exact browser-side extraction used by _read_case.
        tree = ast.parse(textwrap.dedent(inspect.getsource(LiveSelftestCollector._read_case)))
        js = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str) and n.value.startswith('() => Array.from(document.querySelectorAll'))
        sections = self.page.evaluate(js)
        self.assertIn('10⁹/л', sections[0]['text'])
        self.assertIn('H₂O', sections[0]['text'])
        table = next(b for b in sections[0]['content_blocks'] if b['type'] == 'table')
        self.assertEqual(table['rows'][0][1], '10¹²/л')
