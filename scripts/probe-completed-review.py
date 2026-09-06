"""Read-only inspection of the latest completed test review screens."""

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medik_pilot.config import PROBE_DIR, Settings, ensure_directories


settings = Settings.from_env()


def redact_url(value: str) -> str:
    parts = urlsplit(value)
    query = [
        (key, "[redacted]" if key.lower() in {"param", "token", "jsessionid"} else item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def snapshot(page):
    controls = page.locator("button, a, input, [role='button']").evaluate_all(
        """elements => elements.slice(0, 500).map(element => ({
            tag: element.tagName,
            text: (element.innerText || element.value || '').trim().slice(0, 500),
            class_name: String(element.className || '').slice(0, 500),
            href: element.getAttribute('href'),
            disabled: Boolean(element.disabled),
            display: getComputedStyle(element).display,
            visibility: getComputedStyle(element).visibility
        }))"""
    )
    answer_classes = page.locator(".testAnswer").evaluate_all(
        """elements => elements.map(element => ({
            text: (element.innerText || '').trim().slice(0, 500),
            class_name: String(element.className || ''),
            parent_class: String(element.parentElement?.className || ''),
            grandparent_class: String(element.parentElement?.parentElement?.className || '')
        }))"""
    )
    number_controls = page.locator("a, button, td, div, span").evaluate_all(
        """elements => elements.filter(element => /^\\d+$/.test((element.innerText || '').trim()))
            .slice(0, 300).map(element => ({
                tag: element.tagName,
                text: (element.innerText || '').trim(),
                class_name: String(element.className || '').slice(0, 500),
                parent_tag: element.parentElement?.tagName || '',
                parent_class: String(element.parentElement?.className || '').slice(0, 500)
            }))"""
    )
    return {
        "url": redact_url(page.url),
        "text": page.locator("body").inner_text()[:20000],
        "controls": controls,
        "answer_classes": answer_classes,
        "number_controls": number_controls,
    }


def main() -> None:
    if not settings.username or not settings.password:
        raise RuntimeError("SELFTEST_USERNAME and SELFTEST_PASSWORD are required")
    ensure_directories()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        page = browser.new_page()
        page.goto(settings.base_url, wait_until="domcontentloaded", timeout=30000)
        page.locator("#username").fill(settings.username)
        page.locator("#password").fill(settings.password)
        page.locator('input[type="submit"]').click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(10000)
        link = page.locator('a[href*="/spec/qt/"]').first
        link.wait_for(state="attached", timeout=30000)
        page.goto(link.get_attribute("href"), wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(10000)
        result = {"question": snapshot(page)}

        list_button = page.get_by_role("button", name=re.compile(r"[сc]писку вопросов", re.I))
        if list_button.count() == 1 and list_button.is_visible():
            list_button.click()
            page.wait_for_timeout(5000)
            result["question_list"] = snapshot(page)

        result_button = page.get_by_role(
            "button",
            name=re.compile(r"^Результат(?: тестирования)?$", re.I),
        )
        if result_button.count() == 1 and result_button.is_visible():
            result_button.click()
            page.wait_for_timeout(5000)
            result["result"] = snapshot(page)
            number_cells = page.locator("td.qNumber")
            # The XForms repeat keeps one detached/template cell at index 0.
            # The first rendered question is therefore the first connected,
            # visible cell whose text is exactly "1".
            first_number = number_cells.nth(1)
            result["result_number_count"] = number_cells.count()
            if first_number.count() >= 1:
                result["first_number_html"] = first_number.evaluate(
                    "(element) => element.parentElement?.outerHTML || element.outerHTML"
                )
                result["first_number_ancestors"] = first_number.evaluate(
                    """element => {
                        const rows = [];
                        let current = element;
                        for (let index = 0; current && index < 6; index += 1, current = current.parentElement) {
                            rows.push({
                                tag: current.tagName,
                                class_name: String(current.className || ''),
                                id: current.id || '',
                                href: current.getAttribute?.('href'),
                                onclick: current.getAttribute?.('onclick'),
                                role: current.getAttribute?.('role')
                            });
                        }
                        return rows;
                    }"""
                )
                row = first_number.locator("xpath=..")
                row_trigger = row.locator(".qAnswer .xforms-trigger").last
                result["first_row_trigger_count"] = row.locator(".qAnswer .xforms-trigger").count()
                if row_trigger.count() == 1:
                    row_trigger.click(force=True)
                else:
                    first_number.click(force=True)
                page.wait_for_timeout(3000)
                result["result_question"] = snapshot(page)
                result["review_walk"] = []
                for step in range(1, 6):
                    next_button = page.get_by_role("button", name="Далее", exact=True)
                    result["review_walk"].append(
                        {
                            "step": step,
                            "text": page.locator("body").inner_text()[:2000],
                            "next_count": next_button.count(),
                            "next_enabled": (
                                next_button.is_enabled() if next_button.count() == 1 else None
                            ),
                        }
                    )
                    if step >= 5 or next_button.count() != 1 or not next_button.is_enabled():
                        break
                    next_button.click()
                    page.wait_for_timeout(2200)

        (Path(PROBE_DIR) / "completed-review.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        browser.close()


if __name__ == "__main__":
    main()
