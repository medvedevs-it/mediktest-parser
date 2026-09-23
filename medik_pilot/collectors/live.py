import base64
import hashlib
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from ..config import IMAGE_DIR, Settings
from ..domain import (
    CollectedItem,
    extract_case_diagnosis,
    normalize_question_text,
    normalize_text,
)
from ..images import client_image_filename, filename_for_image_content, stored_image_name
from ..specialties import DEFAULT_SPECIALTY, PACKAGE_YEAR, normalize_specialty, package_title


class CollectionConfigurationError(RuntimeError):
    """A run cannot proceed without an explicit account/attempt setting."""


class LiveSelftestCollector:
    """Collect tests and cases from the official trainer.

    Creating attempts and submitting answers are independent, per-run
    permissions. Both are disabled by default.
    """

    # A failed browser session can be recreated without creating a second
    # external attempt: active attempts are resumed and completed attempts are
    # reopened in read-only review mode.
    supports_safe_resume = True

    def __init__(
        self,
        settings: Settings,
        specialty: str = DEFAULT_SPECIALTY,
        max_tests: Optional[int] = None,
        max_cases: Optional[int] = None,
        allow_create_attempts: bool = False,
        allow_answer_submission: bool = False,
        progress_callback: Optional[Callable[[str], None]] = None,
        stop_callback: Optional[Callable[[], bool]] = None,
        pause_callback: Optional[Callable[[], None]] = None,
        capture_callback: Optional[Callable[[CollectedItem, int], None]] = None,
        image_dir: Path = IMAGE_DIR,
    ):
        self.settings = settings
        self.specialty = normalize_specialty(specialty)
        self.package_title = package_title(self.specialty)
        self.package_pattern = re.compile(
            r"(?:РЭ[_\s]*)?{}(?:\s*\([^)]*\))?\s*,?\s*{}".format(
                re.escape(self.specialty), PACKAGE_YEAR
            ),
            re.I,
        )
        self.max_tests = max(0, max_tests) if max_tests is not None else None
        self.max_cases = max(0, max_cases) if max_cases is not None else None
        self.allow_create_attempts = allow_create_attempts
        self.allow_answer_submission = allow_answer_submission
        self.progress_callback = progress_callback or (lambda message: None)
        self.stop_callback = stop_callback or (lambda: False)
        self.pause_callback = pause_callback or (lambda: None)
        self.capture_callback = capture_callback or (lambda item, attempt: None)
        self.image_dir = Path(image_dir)
        self._attempt_number = 0
        self._playwright = None
        self._browser = None
        self._page = None
        self._processed_test_hrefs = set()
        self._processed_case_variants = set()
        # A retry of the same outer attempt must resume the exact attempt that
        # was created for the selected specialty instead of creating another.
        self._test_href_by_attempt: Dict[int, str] = {}
        self._case_link_by_attempt: Dict[int, tuple] = {}

    def close(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        self._page = None
        try:
            if browser:
                browser.close()
        finally:
            if playwright:
                playwright.stop()

    def reset_session(self) -> None:
        """Discard a broken read-only browser session before a safe retry."""
        self.close()

    def _checkpoint(self) -> bool:
        """Wait at a safe material boundary and report a requested stop."""
        self.pause_callback()
        return self.stop_callback()

    def collect_attempt(self, kind: str, attempt: int) -> List[CollectedItem]:
        self._attempt_number = attempt
        if self._checkpoint():
            return []
        if self.allow_create_attempts and not self.allow_answer_submission:
            raise CollectionConfigurationError(
                "Создание новых попыток разрешено, но завершение попыток запрещено. "
                "Для полной выгрузки включите оба разрешения: создание и завершение попыток."
            )
        self._ensure_login()
        if kind == "test":
            return self._collect_tests()
        if kind == "case":
            return self._collect_case()
        raise ValueError("Unknown material kind: {}".format(kind))

    def _capture(self, item: CollectedItem) -> None:
        self.capture_callback(item, self._attempt_number)

    def _first_visible(self, locators, timeout_ms: int = 5000):
        """Return the first visible match across XForms' hidden template copies."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for locator in locators:
                for index in range(locator.count()):
                    candidate = locator.nth(index)
                    try:
                        if candidate.is_visible():
                            return candidate
                    except Exception:
                        continue
            if time.monotonic() >= deadline:
                break
            self._page.wait_for_timeout(250)
        return None

    def _ensure_login(self) -> None:
        if self._page:
            return
        if not self.settings.username or not self.settings.password:
            raise RuntimeError("Заполните SELFTEST_USERNAME и SELFTEST_PASSWORD в файле .env.")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright не установлен. Запустите scripts/setup-windows.ps1.") from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.settings.headless)
        self._page = self._browser.new_page()
        self._page.goto(self.settings.base_url, wait_until="domcontentloaded", timeout=30000)
        self._page.locator("#username").fill(self.settings.username)
        self._page.locator("#password").fill(self.settings.password)
        self._page.locator('input[type="submit"]').click()
        self._page.wait_for_load_state("domcontentloaded", timeout=30000)
        self._page.wait_for_timeout(15000)
        if "login" in self._page.url.lower():
            raise RuntimeError("Тренажёр не принял учётные данные.")

    def _go_home(self) -> None:
        self._page.goto(self.settings.base_url, wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_timeout(10000)

    def _collect_tests(self) -> List[CollectedItem]:
        self._go_home()
        hrefs = [
            href
            for href in self._page.locator('a[href*="/spec/qt/"]').evaluate_all(
                "(links) => links.map(link => link.getAttribute('href'))"
            )
            if href
        ]
        rotate_attempts = self.allow_create_attempts and self.allow_answer_submission
        if rotate_attempts:
            href = self._test_href_by_attempt.get(self._attempt_number)
            if not href:
                href = self._create_test_attempt()
                self._test_href_by_attempt[self._attempt_number] = href
        elif self.specialty != DEFAULT_SPECIALTY:
            # The trainer's list of old attempts does not expose a stable
            # specialty identifier in the URL. Never relabel an unknown old
            # attempt as Pediatrics.
            href = self._create_test_attempt() if self.allow_create_attempts else None
        else:
            href = self._select_test_href(
                hrefs,
                self._processed_test_hrefs,
                rotate_attempts,
            )
        if not href:
            if not self.allow_create_attempts:
                raise CollectionConfigurationError(
                    "Для специальности «{}» не найдена подтверждённая попытка. "
                    "Разрешите создание новых попыток для продолжения.".format(
                        self.specialty
                    )
                )
            href = self._create_test_attempt()
        self._page.goto(href, wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_timeout(15000)
        body_text = self._page.locator("body").inner_text()
        if (
            "Тестирование завершено" in body_text
            or "Тест доступен только для просмотра" in body_text
        ):
            target = self.max_tests if self.max_tests is not None else self.settings.sample_tests
            result = self._review_test_attempt(target)
            self._processed_test_hrefs.add(href)
            return result
        first = self._page.get_by_role("button", name="Перейти к первому вопросу", exact=True)
        if first.count() == 1:
            first.click()
            self._page.wait_for_timeout(10000)
        if self.allow_answer_submission:
            result = self._complete_test_attempt()
            self._processed_test_hrefs.add(href)
            return result
        results = []
        target = self.max_tests if self.max_tests is not None else self.settings.sample_tests
        for index in range(target):
            if self._checkpoint():
                break
            question = self._read_test_question()
            self._capture(question)
            results.append(question)
            self.progress_callback("Тесты: обработан вопрос {}.".format(len(results)))
            if index + 1 >= target:
                break
            next_button = self._page.get_by_role("button", name="Далее", exact=True)
            if next_button.count() != 1 or not next_button.is_enabled():
                break
            previous_text = question.payload["question"]
            next_button.click()
            self._page.wait_for_timeout(3000)
            if normalize_text(self._page.locator(".testQuestion").inner_text()) == previous_text:
                break
        self._processed_test_hrefs.add(href)
        return results

    def _complete_test_attempt(self) -> List[CollectedItem]:
        """Answer an active attempt with a deterministic placeholder and finish it.

        This method is intentionally conservative: it only clicks visible
        controls, stops on an unexpected page state, and never invents a key.
        Correct answers are collected later from the read-only review screen.
        """
        results: List[CollectedItem] = []
        previous_source_id = None
        expected_total = None
        # A new-document run asks for a bounded sample.  The trainer may
        # assign a long attempt (often dozens of questions), but walking the
        # entire attempt would make a 3-question verification run take many
        # minutes and would export more than requested.  Finish after the
        # requested prefix; the review page still discloses the answer key for
        # those answered questions.
        target = self.max_tests if self.max_tests is not None else 200
        for _ in range(200):
            if self._checkpoint():
                break
            question = self._read_test_question()
            if question.payload.get("question_total"):
                expected_total = int(question.payload["question_total"])
            if previous_source_id == question.source_id:
                raise RuntimeError("После нажатия «Далее» тест остался на прежнем вопросе.")
            self._capture(question)
            results.append(question)
            self.progress_callback("Тесты: обработан и подготовлен ответ для вопроса {}.".format(len(results)))
            answers = self._page.locator(".testAnswer")
            if answers.count() < 1:
                raise RuntimeError("На странице попытки не найден вариант ответа.")
            answers.nth(0).click()
            number = question.payload.get("question_number")
            total = question.payload.get("question_total")
            next_button = self._page.get_by_role("button", name="Далее", exact=True)
            if len(results) >= target:
                self._finish_test_attempt()
                break
            if number and total and number < total:
                if next_button.count() != 1 or not next_button.is_enabled():
                    raise RuntimeError("До последнего вопроса теста недоступна кнопка «Далее».")
                previous_source_id = question.source_id
                next_button.click()
                self._page.wait_for_timeout(1500)
                continue
            if number and total and number >= total:
                self._finish_test_attempt()
                break
            # Fallback for a changed header: prefer navigation and only finish
            # when the page no longer exposes an enabled next button.
            if next_button.count() == 1 and next_button.is_enabled():
                previous_source_id = question.source_id
                next_button.click()
                self._page.wait_for_timeout(1500)
                continue
            self._finish_test_attempt()
            break
        else:
            raise RuntimeError("Превышен безопасный предел 200 вопросов в одной попытке.")
        if self.stop_callback() or not results:
            return results
        # A retry may resume an active attempt in the middle. Always review the
        # full question count reported by the trainer so the earlier questions
        # are not lost from the completed package.
        # Review only the requested sample.  ``expected_total`` is the size
        # of the trainer's assigned attempt (often 80+), not the export
        # target; using it here would silently turn a 3-question run into a
        # full-bank collection.
        reviewed = self._review_test_attempt(min(target, expected_total or target))
        if reviewed:
            self.progress_callback("Тесты: из страницы результата извлечено {} ключей.".format(len(reviewed)))
            return reviewed
        self.progress_callback("Тесты: попытка завершена, но страница результата не раскрыла ключи.")
        return results

    def _finish_test_attempt(self) -> None:
        finish = self._page.get_by_role("button", name="Завершить тестирование", exact=True)
        if finish.count() != 1 or not finish.is_visible():
            raise RuntimeError("На последнем вопросе не найдена кнопка завершения тестирования.")
        finish.click()
        self._page.wait_for_timeout(1000)
        confirm = self._page.get_by_role("button", name="Все равно завершить", exact=True)
        if confirm.count() == 1 and confirm.is_visible():
            confirm.click()
        self._page.wait_for_timeout(8000)

    def _review_test_attempt(self, expected: int) -> List[CollectedItem]:
        self._show_test_result_list()
        number_cells = self._page.locator("td.qNumber")
        if number_cells.count() < 2:
            raise RuntimeError("В результате теста не найден список вопросов.")
        expected = min(expected, number_cells.count() - 1)
        # XForms keeps a template row at index 0. Clicking the enabled answer
        # indicator of the first rendered row opens question 1 in read-only
        # review mode.
        first_row = number_cells.nth(1).locator("xpath=..")
        first_trigger = first_row.locator(".qAnswer .xforms-trigger").last
        if first_trigger.count() != 1:
            raise RuntimeError("Не удалось открыть первый вопрос из результата теста.")
        first_trigger.click(force=True)
        first_item = self._wait_for_test_review_question(
            expected_number=1,
            settle_ms=3000,
            timeout_ms=12000,
        )
        if first_item is None:
            raise RuntimeError("После открытия результата не загрузился первый вопрос.")
        reviewed: List[CollectedItem] = []
        for number in range(1, expected + 1):
            if self._checkpoint():
                break
            item = first_item if number == 1 else self._read_test_question()
            if item.payload.get("question_number") not in {None, number}:
                raise RuntimeError(
                    "Нарушен порядок чтения результата теста: ожидался вопрос {}.".format(
                        number
                    )
                )
            if sum(option.get("is_correct") is True for option in item.payload["options"]) != 1:
                raise RuntimeError(
                    "Для вопроса {} не удалось однозначно определить правильный ответ.".format(
                        number
                    )
                )
            self._capture(item)
            reviewed.append(item)
            self.progress_callback(
                "Тесты: прочитан правильный ответ для вопроса {} из {}.".format(
                    number, expected
                )
            )
            if number >= expected:
                continue
            previous_source_id = item.source_id
            if not self._advance_test_review(
                current_number=number,
                previous_source_id=previous_source_id,
            ):
                self.progress_callback(
                    "Тесты: переход после вопроса {} задержался; "
                    "вопрос {} открывается напрямую из списка.".format(
                        number, number + 1
                    )
                )
                self._open_test_review_question(
                    number + 1,
                    previous_source_id=previous_source_id,
                )
        return reviewed

    def _show_test_result_list(self) -> None:
        """Open the completed attempt's question table from any review state."""
        list_button = self._first_visible(
            [
                self._page.get_by_role(
                    "button",
                    name=re.compile(r"[сc]писку вопросов", re.IGNORECASE),
                ),
                self._page.get_by_text(
                    re.compile(r"[кk]\s+[сc]писку вопросов", re.IGNORECASE)
                ),
            ],
            timeout_ms=3000,
        )
        if list_button is not None:
            # XForms does not reliably dispatch its action handler for forced
            # DOM clicks. Use the same trusted click sequence as a user.
            list_button.click()
            self._page.wait_for_timeout(3000)
        result_button = self._first_visible(
            [
                self._page.get_by_role(
                    "button",
                    name=re.compile(r"^Результат(?: тестирования)?$", re.IGNORECASE),
                ),
                self._page.get_by_text(
                    re.compile(r"^Результат(?: тестирования)?$", re.IGNORECASE)
                ),
            ],
            timeout_ms=3000,
        )
        if result_button is not None:
            result_button.click()
            # The repeated rows enter the DOM before XForms binds their action
            # handlers. Keep a short readiness window before clicking a row.
            self._page.wait_for_timeout(3000)
        number_cells = self._page.locator("td.qNumber")
        deadline = time.monotonic() + 12
        while number_cells.count() < 2 and time.monotonic() < deadline:
            self._page.wait_for_timeout(250)

    def _wait_for_test_review_question(
        self,
        expected_number: int,
        previous_source_id: Optional[str] = None,
        settle_ms: int = 0,
        timeout_ms: int = 12000,
    ) -> Optional[CollectedItem]:
        """Wait until XForms has committed the requested review question."""
        if settle_ms > 0:
            # XForms commits transitions on its own event loop. Repeated DOM
            # reads immediately after the click can delay that commit.
            self._page.wait_for_timeout(settle_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._checkpoint():
                return None
            try:
                item = self._read_test_question()
                actual_number = item.payload.get("question_number")
                number_matches = actual_number in {None, expected_number}
                source_changed = (
                    previous_source_id is None
                    or item.source_id != previous_source_id
                )
                if number_matches and source_changed:
                    return item
            except Exception:
                # The old and new XForms fragments can briefly coexist while a
                # transition is being committed.
                pass
            self._page.wait_for_timeout(250)
        return None

    def _advance_test_review(
        self,
        current_number: int,
        previous_source_id: str,
    ) -> bool:
        next_button = self._page.get_by_role("button", name="Далее", exact=True)
        if (
            next_button.count() != 1
            or not next_button.is_visible()
            or not next_button.is_enabled()
        ):
            return False
        next_button.click()
        return (
            self._wait_for_test_review_question(
                expected_number=current_number + 1,
                previous_source_id=previous_source_id,
                settle_ms=2500,
                timeout_ms=6000,
            )
            is not None
        )

    def _open_test_review_question(
        self,
        number: int,
        previous_source_id: Optional[str] = None,
    ) -> CollectedItem:
        """Recover a delayed Next action by selecting the exact result row."""
        self._show_test_result_list()
        number_cells = self._page.locator("td.qNumber")
        deadline = time.monotonic() + 12
        while number_cells.count() <= number and time.monotonic() < deadline:
            self._page.wait_for_timeout(250)
        if number_cells.count() <= number:
            raise RuntimeError(
                "В списке результата не найден вопрос {}.".format(number)
            )
        row = number_cells.nth(number).locator("xpath=..")
        triggers = row.locator(".qAnswer .xforms-trigger")
        if triggers.count() < 1:
            raise RuntimeError(
                "Не удалось открыть вопрос {} из списка результата.".format(number)
            )
        # Each row contains disabled status-icon triggers followed by the real
        # navigation trigger. The trainer's XForms markup keeps all of them in
        # the DOM, so visibility alone cannot identify the actionable control.
        trigger = triggers.last
        trigger.click(force=True)
        item = self._wait_for_test_review_question(
            expected_number=number,
            previous_source_id=previous_source_id,
            settle_ms=3000,
            timeout_ms=12000,
        )
        if item is None:
            raise RuntimeError(
                "После прямого открытия вопрос {} не загрузился.".format(number)
            )
        return item

    def _read_test_question(self) -> CollectedItem:
        question_locator = self._page.locator(".testQuestion")
        answers_locator = self._page.locator(".testAnswer")
        letters_locator = self._page.locator(".testLetter")
        if question_locator.count() != 1 or answers_locator.count() < 2:
            raise RuntimeError("Не удалось распознать карточку тестового вопроса.")
        question = normalize_question_text(question_locator.inner_text())
        answers = [normalize_text(value) for value in answers_locator.all_inner_texts()]
        letters = [normalize_text(value) for value in letters_locator.all_inner_texts()]
        identity = hashlib.sha256(
            (question + "\n" + "\n".join(answers)).encode("utf-8")
        ).hexdigest()[:24]
        header_match = re.search(r"Вопрос\s+(\d+)\s+из\s+(\d+)", self._page.locator("body").inner_text())
        option_rows = []
        for index, answer in enumerate(answers):
            locator = answers_locator.nth(index)
            own_class = str(locator.get_attribute("class") or "")
            parent_class = str(locator.locator("..").get_attribute("class") or "")
            option_rows.append(
                {
                    "letter": letters[index] if index < len(letters) else None,
                    "text": answer,
                    "is_correct": (
                        True
                        if (
                            "text-success" in own_class
                            or "correct_answer" in own_class
                            or "text-success" in parent_class
                            or "correct_answer" in parent_class
                        )
                        else None
                    ),
                }
            )
        disclosed = any(option["is_correct"] is True for option in option_rows)
        if disclosed:
            for option in option_rows:
                if option["is_correct"] is None:
                    option["is_correct"] = False
        payload: Dict = {
            "specialty": self.specialty,
            "question": question,
            "question_number": int(header_match.group(1)) if header_match else None,
            "question_total": int(header_match.group(2)) if header_match else None,
            "options": option_rows,
            "single_answer": True,
            "correct_answer_status": (
                "disclosed_after_answered_attempt" if disclosed else "hidden_until_attempt_completion"
            ),
            "synthetic": False,
        }
        raw_payload = {
            "url": self._page.url,
            "question_html": question_locator.inner_html(),
            "answers_html": [answers_locator.nth(index).inner_html() for index in range(answers_locator.count())],
        }
        return CollectedItem(
            kind="test",
            source_id="live-test-{}".format(identity),
            payload=payload,
            raw_payload=raw_payload,
        )

    def _create_test_attempt(self) -> str:
        self._go_home()
        existing_hrefs = {
            href
            for href in self._page.locator('a[href*="/spec/qt/"]').evaluate_all(
                "(links) => links.map(link => link.getAttribute('href'))"
            )
            if href
        }
        candidates = [
            self._page.get_by_role(
                "button",
                name=re.compile(r"Решать тест|Начать тест|Пройти тестирование", re.I),
            ),
            self._page.get_by_text("Пройти тестирование", exact=True),
            self._page.get_by_text(
                re.compile(r"Решать тест|Начать тест|Пройти тестирование", re.I)
            ),
        ]
        start = self._first_visible(candidates)
        if start is None:
            raise RuntimeError("Не найдена кнопка создания тестовой попытки.")
        start.click()
        self._page.wait_for_timeout(5000)
        package = self._first_visible(
            [
                self._page.get_by_role(
                    "button",
                    name=self.package_pattern,
                ),
                self._page.get_by_text(self.package_title, exact=True),
                self._page.get_by_text(self.package_pattern),
            ]
        )
        if package is None:
            raise RuntimeError("Не найден пакет «{}».".format(self.package_title))
        package.click()
        self._page.wait_for_timeout(8000)
        if "/spec/qt/" in self._page.url:
            return self._page.url
        links = self._page.locator('a[href*="/spec/qt/"]')
        links.first.wait_for(state="attached", timeout=30000)
        hrefs = [
            href
            for href in links.evaluate_all(
                "(nodes) => nodes.map(node => node.getAttribute('href'))"
            )
            if href
        ]
        href = next((candidate for candidate in hrefs if candidate not in existing_hrefs), None)
        if not href and hrefs:
            href = hrefs[0]
        if not href:
            raise RuntimeError("После создания тестовой попытки не получена ссылка.")
        return href

    def _collect_case(self) -> List[CollectedItem]:
        self._go_home()
        navigation = self._page.get_by_text("Мультикейс", exact=True)
        if navigation.count() == 0:
            navigation = self._page.locator("text=Мультикейс")
        try:
            navigation.first.wait_for(state="visible", timeout=30000)
        except Exception as exc:
            raise RuntimeError("Раздел «Мультикейс» не найден.") from exc
        navigation.first.click()
        self._page.wait_for_timeout(7000)
        links = self._case_links()
        rotate_attempts = self.allow_create_attempts and self.allow_answer_submission
        if rotate_attempts:
            pair = self._case_link_by_attempt.get(self._attempt_number)
            if pair is None:
                pair = self._create_case_attempt()
                self._case_link_by_attempt[self._attempt_number] = pair
            links = [pair]
        elif self.specialty != DEFAULT_SPECIALTY:
            links = [self._create_case_attempt()] if self.allow_create_attempts else []
        else:
            links = self._select_case_links(
                links,
                self._processed_case_variants,
                rotate_attempts,
                self.max_cases,
            )
        items: List[CollectedItem] = []
        seen = set()
        for href, variant_id in links:
            if self._checkpoint():
                break
            if self.max_cases is not None and len(items) >= self.max_cases or not variant_id or variant_id in seen:
                continue
            try:
                self._open_case(href)
                item = self._read_case(variant_id)
            except Exception as exc:
                if rotate_attempts:
                    raise RuntimeError(
                        "Не удалось прочитать созданную задачу {}: {}".format(
                            variant_id, exc
                        )
                    ) from exc
                self.progress_callback("Ситуационные задачи: кейс {} пропущен: {}.".format(variant_id, exc))
                continue
            items.append(item)
            self._capture(item)
            seen.add(variant_id)
            self._processed_case_variants.add(variant_id)
            self.progress_callback("Ситуационные задачи: обработан кейс {} из {}.".format(len(items), self.max_cases))

        # If the account has fewer open cases than requested, create additional
        # attempts through the normal UI and collect the newly assigned variant.
        if not self.allow_create_attempts:
            if self.specialty != DEFAULT_SPECIALTY and not items:
                raise CollectionConfigurationError(
                    "Для специальности «{}» не найдена подтверждённая задача. "
                    "Разрешите создание новых попыток для продолжения.".format(
                        self.specialty
                    )
                )
            return items
        if self.max_cases is None:
            return items
        for _ in range(max(0, self.max_cases - len(items))):
            if self._checkpoint():
                break
            href, variant_id = self._create_case_attempt()
            if not variant_id or variant_id in seen:
                continue
            self._open_case(href)
            item = self._read_case(variant_id)
            items.append(item)
            self._capture(item)
            seen.add(variant_id)
            self._processed_case_variants.add(variant_id)
            self.progress_callback("Ситуационные задачи: обработан кейс {} из {}.".format(len(items), self.max_cases))
        return items

    def _case_links(self):
        return [
            (link.get_attribute("href"), parse_qs(urlparse(link.get_attribute("href")).query).get("variant", [""])[0])
            for link in self._page.locator('a[href*="/spec/mt/"]').all()
            if link.get_attribute("href")
        ]

    @staticmethod
    def _select_test_href(
        hrefs: List[str],
        processed: set,
        rotate_attempts: bool,
    ) -> Optional[str]:
        if not rotate_attempts:
            return hrefs[0] if hrefs else None
        return next((href for href in hrefs if href not in processed), None)

    @staticmethod
    def _select_case_links(
        links: List[tuple],
        processed: set,
        rotate_attempts: bool,
        limit: Optional[int],
    ) -> List[tuple]:
        selected = (
            [pair for pair in links if pair[1] not in processed]
            if rotate_attempts
            else list(links)
        )
        return selected if limit is None else selected[:limit]

    def _open_case(self, href: str) -> None:
        self._page.goto(href, wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_timeout(10000)
        options = self._page.locator(".custom-control-label").first
        try:
            options.wait_for(state="visible", timeout=5000)
        except Exception:
            first = self._page.get_by_role("button", name="Перейти к первому вопросу", exact=True)
            if first.count() != 1:
                first = self._page.get_by_text("Перейти к первому вопросу", exact=True)
            first.wait_for(state="visible", timeout=30000)
            first.click()
            options.wait_for(state="visible", timeout=30000)

    def _create_case_attempt(self):
        self._go_home()
        navigation = self._page.get_by_text("Мультикейс", exact=True)
        if navigation.count() == 0:
            navigation = self._page.locator("text=Мультикейс")
        navigation.first.click()
        self._page.wait_for_timeout(7000)
        existing_variants = {
            variant_id for _, variant_id in self._case_links() if variant_id
        }
        start = self._page.get_by_role("button", name="Решать кейс", exact=True)
        start.wait_for(state="visible", timeout=30000)
        start.click()
        self._page.wait_for_timeout(5000)
        package = self._first_visible(
            [
                self._page.get_by_role(
                    "button",
                    name=self.package_pattern,
                ),
                self._page.get_by_text(self.package_title, exact=True),
                self._page.get_by_text(self.package_pattern),
            ]
        )
        if package is None:
            raise RuntimeError(
                "Не найден пакет «{}» для кейса.".format(self.package_title)
            )
        package.click()
        self._page.wait_for_timeout(8000)
        links = self._case_links()
        if not links:
            raise RuntimeError("После создания ситуационной задачи не получена ссылка.")
        href, variant_id = next(
            (
                pair
                for pair in links
                if pair[1] and pair[1] not in existing_variants
            ),
            links[0],
        )
        return href, variant_id

    def _read_case(self, variant_id: str) -> CollectedItem:
        self._page.locator(".custom-control-label").first.wait_for(state="visible", timeout=30000)
        if self.allow_answer_submission and self._case_answers_enabled():
            self._complete_case_attempt()
        body_text = self._page.locator("body").inner_text()
        option_locator = self._page.locator(".custom-control-label")
        options = [normalize_text(value) for value in option_locator.all_inner_texts() if normalize_text(value)]
        if len(options) < 2:
            raise RuntimeError("Не удалось распознать варианты первого вопроса кейса.")
        sections = self._page.evaluate(
            r"""() => Array.from(document.querySelectorAll('a.nav-link[href^="#i"]')).map(link => {
                const id = (link.getAttribute('href') || '').slice(1);
                const target = id ? document.getElementById(id) : null;
                const images = [];
                const contentBlocks = [];
                // Keep mathematical notation before any innerText conversion.
                const mathText = node => {
                    const copy = node.cloneNode(true);
                    for (const el of copy.querySelectorAll('sup, sub')) {
                        const from = '0123456789+-=()';
                        const to = el.tagName === 'SUP' ? '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾' : '₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎';
                        el.replaceWith(Array.from(el.textContent).map(c => from.includes(c) ? to[from.indexOf(c)] : c).join(''));
                    }
                    for (const br of copy.querySelectorAll('br')) br.replaceWith(' ');
                    return copy.textContent || '';
                };
                let textParts = [];
                const ignoredTags = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
                const blockTags = new Set([
                    'ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'DIV', 'DL', 'FIELDSET',
                    'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM', 'H1', 'H2', 'H3', 'H4',
                    'H5', 'H6', 'HEADER', 'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P',
                    'PRE', 'SECTION', 'UL'
                ]);
                const flushText = () => {
                    const text = textParts.join('').replace(/\s+/g, ' ').trim();
                    textParts = [];
                    if (!text) return;
                    const previous = contentBlocks[contentBlocks.length - 1];
                    if (previous && previous.type === 'text') previous.text += ' ' + text;
                    else contentBlocks.push({type: 'text', text});
                };
                const visit = node => {
                    if (node.nodeType === Node.TEXT_NODE) {
                        textParts.push(node.textContent || '');
                        return;
                    }
                    if (node.nodeType !== Node.ELEMENT_NODE || ignoredTags.has(node.tagName)) return;
                    if (node.tagName === 'SUP' || node.tagName === 'SUB') {
                        const wrapper = document.createElement('span');
                        wrapper.appendChild(node.cloneNode(true));
                        textParts.push(mathText(wrapper));
                        return;
                    }
                    if (node.tagName === 'BR') {
                        textParts.push(' ');
                        return;
                    }
                    if (node.tagName === 'IMG') {
                        flushText();
                        const image = {
                            source_url: node.currentSrc || node.getAttribute('src') || node.getAttribute('data-src') || node.getAttribute('data-original') || '',
                            suggested_name: node.getAttribute('data-filename') || node.getAttribute('download') || '',
                            alt: (node.getAttribute('alt') || '').trim(),
                            order: images.length + 1,
                        };
                        if (image.source_url) {
                            images.push(image);
                            contentBlocks.push({type: 'image', image_order: image.order});
                        }
                        return;
                    }
                    if (node.tagName === 'TABLE') {
                        flushText();
                        const rows = Array.from(node.rows || []).map(row =>
                            Array.from(row.cells || []).map(cell => mathText(cell).trim())
                        ).filter(row => row.some(cell => cell));
                        if (rows.length) contentBlocks.push({type: 'table', rows});
                        return;
                    }
                    const isBlock = blockTags.has(node.tagName);
                    if (isBlock) flushText();
                    Array.from(node.childNodes || []).forEach(visit);
                    if (isBlock) flushText();
                };
                if (target) {
                    Array.from(target.childNodes || []).forEach(visit);
                    flushText();
                }
                return {
                    title: (link.innerText || '').trim(),
                    text: target ? mathText(target).trim() : '',
                    html: target ? (target.innerHTML || '').trim() : '',
                    images,
                    content_blocks: contentBlocks,
                };
            }).filter(item => item.title && (item.text || item.images.length || item.content_blocks.length))"""
        )
        section_blocks = []
        seen = set()
        for section in sections:
            text = normalize_text(section.get("text", ""))
            title = normalize_text(section.get("title", ""))
            images = section.get("images") or []
            content_blocks = section.get("content_blocks") or []
            key = (
                title,
                text,
                repr(content_blocks),
                tuple(str(image.get("source_url") or "") for image in images),
            )
            if (text or images) and key not in seen:
                section_blocks.append({
                    "title": title,
                    "text": text,
                    "content_blocks": list(content_blocks),
                    "images": list(images),
                })
                seen.add(key)
        condition = "\n\n".join(
            "{}\n{}".format(item["title"], item["text"]) for item in section_blocks
        )
        if not condition:
            condition = self._extract_case_condition(body_text)
        source_id = "live-case-{}".format(variant_id or hashlib.sha256(condition.encode("utf-8")).hexdigest()[:24])
        downloaded_count = self._download_case_images(section_blocks)
        if downloaded_count:
            self.progress_callback(
                "Ситуационные задачи: сохранено изображений для текущего кейса — {}.".format(
                    downloaded_count
                )
            )
        questions = []
        question_numbers = self._case_question_numbers()
        for question_number in question_numbers:
            if self._checkpoint():
                break
            # A completed attempt may reopen on the last viewed question.
            # Always select the explicit number so exported ordering cannot
            # depend on that external UI state.
            self._open_case_question(question_number)
            questions.append(self._read_case_question(question_number))
            partial_payload = {
                "specialty": self.specialty,
                "condition": condition,
                "condition_sections": section_blocks,
                "information_blocks": [
                    "{}: {}".format(item["title"], item["text"]) for item in section_blocks
                ],
                "questions": list(questions),
                "sampled_questions": len(questions),
                "expected_questions": len(question_numbers),
                "synthetic": False,
            }
            partial_header = extract_case_diagnosis(partial_payload)
            if partial_header:
                partial_payload["header"] = partial_header
            self._capture(
                CollectedItem(
                    kind="case",
                    source_id=source_id,
                    payload=partial_payload,
                    raw_payload={
                        "url": self._page.url,
                        "body_text": body_text,
                        "condition_sections": sections,
                    },
                )
            )

        payload = {
            "specialty": self.specialty,
            "condition": condition,
            "condition_sections": section_blocks,
            "information_blocks": ["{}: {}".format(item["title"], item["text"]) for item in section_blocks],
            "questions": questions,
            "sampled_questions": len(questions),
            "expected_questions": len(question_numbers),
            "synthetic": False,
        }
        header = extract_case_diagnosis(payload)
        if header:
            payload["header"] = header
        return CollectedItem(
            kind="case",
            source_id=source_id,
            payload=payload,
            raw_payload={
                "url": self._page.url,
                "body_text": body_text,
                "condition_sections": sections,
            },
        )

    def _download_case_images(self, sections: List[Dict]) -> int:
        downloaded = 0
        for section in sections:
            prepared = []
            for image in section.get("images") or []:
                asset = self._download_image_asset(image)
                prepared.append(asset)
                if asset.get("downloaded") is True:
                    downloaded += 1
                else:
                    self.progress_callback(
                        "Ситуационные задачи: изображение не сохранено: {}.".format(
                            asset.get("error") or asset.get("source_url") or "неизвестный источник"
                        )
                    )
            section["images"] = prepared
        return downloaded

    def _download_image_asset(self, image: Dict) -> Dict:
        source_url = urljoin(self._page.url, str(image.get("source_url") or "").strip())
        parsed_url = urlparse(source_url)
        public_url = (
            "data:image"
            if source_url.startswith("data:image/")
            else parsed_url._replace(query="", fragment="").geturl()
        )
        result = {
            "asset_kind": "image",
            "source_url": public_url,
            "alt": normalize_text(str(image.get("alt") or "")),
            "order": int(image.get("order") or 0),
            "downloaded": False,
        }
        if not source_url:
            return {**result, "error": "пустой URL"}
        try:
            content_type = ""
            if source_url.startswith("data:image/"):
                header, encoded = source_url.split(",", 1)
                content_type = header[5:].split(";", 1)[0]
                content = base64.b64decode(encoded) if ";base64" in header else encoded.encode("utf-8")
            else:
                last_error = None
                for attempt in range(1, 4):
                    try:
                        response = self._page.context.request.get(source_url, timeout=30000)
                        if not response.ok:
                            raise RuntimeError("HTTP {}".format(response.status))
                        content_type = str(response.headers.get("content-type") or "")
                        content = response.body()
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < 3:
                            self._page.wait_for_timeout(500 * attempt)
                else:
                    raise RuntimeError(str(last_error or "ошибка загрузки"))
            if not content:
                raise RuntimeError("пустой файл")
            filename = client_image_filename(
                source_url,
                str(image.get("suggested_name") or ""),
                content_type,
            )
            filename, detected_content_type = filename_for_image_content(filename, content)
            normalized_content_type = (
                detected_content_type
                or content_type.split(";", 1)[0].strip()
            )
            storage_name, digest = stored_image_name(content, filename)
            self.image_dir.mkdir(parents=True, exist_ok=True)
            target = self.image_dir / storage_name
            if not target.exists():
                target.write_bytes(content)
            return {
                **result,
                "filename": filename,
                "storage_name": storage_name,
                "sha256": digest,
                "content_type": normalized_content_type,
                "size": len(content),
                "downloaded": True,
            }
        except Exception as exc:
            fallback_name = client_image_filename(
                source_url,
                str(image.get("suggested_name") or ""),
            )
            return {**result, "filename": fallback_name, "error": str(exc)}

    def _case_answers_enabled(self) -> bool:
        inputs = self._page.locator(".custom-control-input")
        return inputs.count() > 0 and not inputs.first.is_disabled()

    def _complete_case_attempt(self) -> None:
        question_numbers = self._case_question_numbers()
        self.progress_callback(
            "Ситуационные задачи: подтверждена активная попытка, обрабатывается {} вопросов.".format(
                len(question_numbers)
            )
        )
        for index, question_number in enumerate(question_numbers):
            if self._checkpoint():
                return
            # Always open the explicit number. This makes a browser-session
            # retry safe even when the external attempt stopped in the middle.
            self._open_case_question(question_number)
            labels = self._page.locator(".custom-control-label")
            if labels.count() < 1:
                raise RuntimeError("В кейсе не найден вариант ответа для вопроса {}.".format(question_number))
            question_text = normalize_question_text(self._case_question_html())
            input_types = self._page.locator(".custom-control-input").evaluate_all(
                "(nodes) => nodes.map(node => (node.type || '').toLowerCase())"
            )
            required = self._required_case_answer_count(
                question_text,
                input_types,
                labels.count(),
            )
            inputs = self._page.locator(".custom-control-input")
            answers_enabled = any(
                not inputs.nth(option_index).is_disabled()
                for option_index in range(inputs.count())
            )
            if answers_enabled:
                # Preserve already selected placeholder answers on resume and
                # normalize checkbox selections to the required count.
                for option_index in range(inputs.count()):
                    should_be_checked = option_index < required
                    if inputs.nth(option_index).is_checked() != should_be_checked:
                        labels.nth(option_index).click()
                self._wait_for_case_answer_selection(required)
            is_last_question = index + 1 >= len(question_numbers)
            if is_last_question:
                finish_button = self._first_visible(
                    [
                        self._page.get_by_role(
                            "button",
                            name=re.compile(
                                r"Завершить решение кейса|Завершить кейс|^Завершить$",
                                re.I,
                            ),
                        ),
                        self._page.get_by_text(
                            re.compile(
                                r"Завершить решение кейса|Завершить кейс|^Завершить$",
                                re.I,
                            )
                        ),
                    ],
                    timeout_ms=10000,
                )
                if finish_button is None:
                    raise RuntimeError(
                        "После ответа на последний вопрос не найдена кнопка "
                        "завершения кейса."
                    )
                finish_button.click(force=True)
                self._page.wait_for_timeout(8000)
                continue
            # The next iteration opens an explicit number, so an already
            # answered question does not need another mutating Next click.
            if answers_enabled:
                next_button = self._first_visible(
                    [
                        self._page.locator("#next"),
                        self._page.get_by_role("button", name="Далее", exact=True),
                        self._page.get_by_text("Далее", exact=True),
                    ],
                    timeout_ms=10000,
                )
                if next_button is None:
                    raise RuntimeError(
                        "После выбора ответа не найден видимый переход «Далее» "
                        "для вопроса {}.".format(question_number)
                    )
                next_button.click()
                self._page.wait_for_timeout(1800)
                self._check_case_answer_dialog(required, next_button)
        result_button = self._first_visible(
            [
                self._page.get_by_role("button", name="Результат", exact=True),
                self._page.get_by_text("Результат", exact=True),
            ],
            timeout_ms=3000,
        )
        if result_button is not None:
            result_button.click(force=True)
            self._page.wait_for_timeout(3000)

    def _wait_for_case_answer_selection(self, required: int) -> None:
        self._page.wait_for_function(
            "n => document.querySelectorAll('.custom-control-input:checked').length === n",
            arg=required, timeout=10000,
        )
        # The trainer validates Vue state, not only the browser's checked
        # property. Allow its queued render/update to commit before Next.
        self._page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    def _check_case_answer_dialog(self, required: int, next_button) -> None:
        dialogs = self._page.locator('[role="dialog"]:visible')
        if not dialogs.count():
            return
        if dialogs.count() != 1:
            raise RuntimeError("Несколько окон тренажёра перекрывают вопрос кейса.")
        dialog = dialogs.first
        text = normalize_text(dialog.inner_text())
        # Normal case workflow reveals investigation results after an answer.
        # Acknowledge only this observed informational popup; Next has already
        # been submitted, so do not submit it again.
        if text.startswith('Доступны новые данные ×'):
            close = dialog.locator('button').filter(has_text=re.compile(r'^\s*×\s*$'))
            if close.count() != 1:
                raise RuntimeError("Не найдена кнопка закрытия новых данных кейса.")
            close.click()
            dialog.wait_for(state='hidden', timeout=10000)
            return
        counts = re.search(
            r"необходимо выбрать (\d+) вариантов ответа\. Выбрано вариантов ответа: (\d+)", text
        )
        # A reproduced transient validation popup reports equal counts after
        # the reactive state catches up. Never dismiss other warnings or limits.
        if not text.startswith('Неверное количество вариантов ответа') or not counts or tuple(map(int, counts.groups())) != (required, required):
            raise RuntimeError("Тренажёр отклонил выбор ответов: " + text)
        close = dialog.locator('button').filter(has_text=re.compile(r'^\s*×\s*$'))
        if close.count() != 1:
            raise RuntimeError("Не найдена однозначная кнопка закрытия сообщения о количестве ответов.")
        close.click()
        dialog.wait_for(state='hidden', timeout=10000)
        self._wait_for_case_answer_selection(required)
        next_button.click()
        self._page.wait_for_timeout(1800)
        if self._page.locator('[role="dialog"]:visible').count():
            raise RuntimeError("Повторное отклонение ответов тренажёром; сбор остановлен.")

    @staticmethod
    def _required_case_answer_count(
        question_text: str,
        input_types: List[str],
        option_count: int,
    ) -> int:
        if not any(value == "checkbox" for value in input_types):
            return 1
        match = re.search(r"выберите\s+(\d+)", question_text, re.IGNORECASE)
        requested = int(match.group(1)) if match else 1
        return max(1, min(requested, option_count))

    def _case_question_numbers(self) -> List[int]:
        numbers = []
        for value in self._page.locator("a.page-link, button").all_inner_texts():
            text = normalize_text(value)
            if text.isdigit():
                number = int(text)
                if 1 <= number <= 100 and number not in numbers:
                    numbers.append(number)
        numbers.sort()
        if numbers and numbers[0] == 1:
            return numbers
        return list(range(1, max(1, self.settings.case_questions) + 1))

    def _open_case_question(self, question_number: int) -> None:
        """Open a numbered case question without selecting an answer."""
        dialogs = self._page.locator('[role="dialog"]:visible')
        if dialogs.count():
            text = normalize_text(dialogs.first.inner_text())
            if dialogs.count() != 1 or not re.fullmatch(
                r'Результаты решения задачи × Вы ответили верно на \d+ вопрос(?:а|ов)? из \d+\.', text
            ):
                raise RuntimeError("Окно тренажёра требует проверки: " + text)
            close = dialogs.first.locator('button').filter(has_text=re.compile(r'^\s*×\s*$'))
            if close.count() != 1:
                raise RuntimeError("Не найдена кнопка закрытия результатов кейса.")
            close.click()
            dialogs.first.wait_for(state='hidden', timeout=10000)
        button = self._page.locator('nav[aria-label="Список вопросов"] a.page-link').filter(
            has_text=re.compile(r"^\s*{}\s*$".format(question_number))
        )
        if button.count() != 1:
            raise RuntimeError("Не удалось найти кнопку вопроса {} кейса.".format(question_number))
        # Do not click arbitrary matching text or force a disabled pagination
        # item: either can leave the previous question in the export.
        button.click(timeout=30000)
        self._page.wait_for_function(
            """number => {
                const active = document.querySelector('nav[aria-label="Список вопросов"] li.active a.page-link');
                return active && active.textContent.trim() === String(number)
                    && document.querySelector('h5.adoc')
                    && document.querySelectorAll('.custom-control-label').length >= 2;
            }""", arg=question_number, timeout=30000,
        )

    def _case_question_html(self) -> str:
        # The heading is in the same question panel as the answer controls,
        # not in the condition tabs (where option text often occurs as well).
        panel = self._page.locator('.custom-control-label').first.locator(
            'xpath=ancestor::div[.//h5[contains(@class,"adoc")]][1]'
        )
        heading = panel.locator('h5.adoc')
        if heading.count() != 1:
            raise RuntimeError("Не найден однозначный текст вопроса кейса.")
        return heading.inner_html()

    def _read_case_question(self, question_number: int) -> Dict:
        option_locator = self._page.locator(".custom-control-label")
        options = [normalize_text(value) for value in option_locator.all_inner_texts() if normalize_text(value)]
        if len(options) < 2:
            raise RuntimeError("Не удалось распознать варианты вопроса {} кейса.".format(question_number))
        active = self._page.locator('nav[aria-label="Список вопросов"] li.active a.page-link')
        if active.count() != 1 or active.inner_text().strip() != str(question_number):
            raise RuntimeError("Номер открытого вопроса кейса не совпадает с ожидаемым.")
        question_text = normalize_question_text(self._case_question_html())
        if not question_text:
            raise RuntimeError("Пустой текст вопроса кейса.")
        option_rows = []
        for label in option_locator.all():
            text = normalize_text(label.inner_html())
            own_class = str(label.get_attribute("class") or "")
            parent_class = str(label.locator(".." ).get_attribute("class") or "")
            option_rows.append({
                "text": text,
                "is_correct": (
                    True
                    if (
                        "text-success" in own_class
                        or "correct_answer" in own_class
                        or "text-success" in parent_class
                        or "correct_answer" in parent_class
                    )
                    else None
                ),
            })
        disclosed = any(item["is_correct"] is True for item in option_rows)
        if disclosed:
            for item in option_rows:
                if item["is_correct"] is None:
                    item["is_correct"] = False
        input_types = self._page.locator(".custom-control-input").evaluate_all(
            "(nodes) => nodes.map(node => (node.type || '').toLowerCase())"
        )
        correct_count = sum(item["is_correct"] is True for item in option_rows)
        expected_answer_count = self._required_case_answer_count(
            question_text,
            input_types,
            len(option_rows),
        )
        return {
            "section": None,
            "question_number": question_number,
            "question": question_text,
            "multiple": (
                correct_count > 1 if disclosed else expected_answer_count > 1
            ),
            "options": option_rows,
            "correct_answer_status": "disclosed_after_completed_attempt" if disclosed else "hidden_until_attempt_completion",
        }

    @staticmethod
    def _extract_case_question(body_text: str, first_option: str) -> str:
        normalized_lines = [line.strip() for line in body_text.splitlines()]
        try:
            option_index = normalized_lines.index(first_option)
        except ValueError:
            return ""
        for index in range(option_index - 1, -1, -1):
            candidate = normalized_lines[index]
            if candidate and candidate not in {"Далее", "П", "Д", "Л", "В"} and not candidate.isdigit():
                return normalize_text(candidate)
        return ""

    @staticmethod
    def _extract_case_condition(body_text: str) -> str:
        marker = re.search(r"\n1\s*\n2\s*\n3\s*\n4", body_text)
        return normalize_text(body_text[:marker.start()] if marker else body_text)
