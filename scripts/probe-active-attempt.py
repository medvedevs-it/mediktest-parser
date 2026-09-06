"""Read-only probe of an already active selftest attempt.

The script never chooses an answer, advances a question, or finishes an attempt.
Generated artifacts live under ignored data/probes/.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medik_pilot.config import PROBE_DIR, Settings, ensure_directories


settings = Settings.from_env()


def redact(value: str) -> str:
    value = value.replace(settings.username, "[account]")
    value = value.replace(settings.password, "[secret]")
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}", "[email]", value)
    return value


def redact_url(value: str) -> str:
    parts = urlsplit(value)
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "[redacted]" if key.lower() in {"param", "token", "jsessionid"} else item))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def snapshot_frame(frame):
    try:
        text = redact(frame.locator("body").inner_text(timeout=5000))[:40000]
        controls = frame.locator(
            "button, a, input, select, textarea, [role='button'], [onclick], .x-btn"
        ).evaluate_all(
            """elements => elements.slice(0, 500).map(element => ({
                tag: element.tagName,
                text: (element.innerText || element.value || '').trim().slice(0, 1000),
                id: element.id || null,
                class_name: String(element.className || '').slice(0, 500),
                role: element.getAttribute('role'),
                type: element.getAttribute('type'),
                name: element.getAttribute('name'),
                href: element.getAttribute('href')
            }))"""
        )
    except Exception as exc:
        text = "[unreadable: {}]".format(type(exc).__name__)
        controls = []
    return {"name": frame.name, "url": redact_url(frame.url), "visible_text": text, "controls": controls}


def selector_state(page, selector: str):
    return page.locator(selector).evaluate_all(
        """elements => elements.map(element => ({
            tag: element.tagName,
            text: (element.innerText || element.value || '').trim().slice(0, 300),
            type: element.getAttribute('type'),
            disabled: Boolean(element.disabled),
            checked: Boolean(element.checked),
            aria_disabled: element.getAttribute('aria-disabled'),
            display: getComputedStyle(element).display,
            visibility: getComputedStyle(element).visibility
        }))"""
    )


def main() -> None:
    if not settings.username or not settings.password:
        raise RuntimeError("SELFTEST_USERNAME and SELFTEST_PASSWORD are required")
    ensure_directories()
    traffic = []
    messages = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        page = browser.new_page()

        def record_response(response):
            request = response.request
            if request.resource_type not in {"document", "xhr", "fetch"}:
                return
            if "selftest.mededtech.ru" not in response.url:
                return
            entry = {
                "method": request.method,
                "resource_type": request.resource_type,
                "status": response.status,
                "url": redact_url(response.url),
                "content_type": response.headers.get("content-type", "").split(";", 1)[0],
            }
            if "/spec/qt/" in response.url or response.url.endswith("/secured/data"):
                entry["request_preview"] = redact((request.post_data or "")[:12000])
                try:
                    entry["response_preview"] = redact(response.text()[:40000])
                except Exception:
                    entry["response_preview"] = "[unreadable]"
            traffic.append(entry)

        page.on("response", record_response)
        page.on("console", lambda message: messages.append({"type": message.type, "text": redact(message.text)[:3000]}))
        page.on("pageerror", lambda error: messages.append({"type": "pageerror", "text": redact(str(error))[:5000]}))
        page.goto(settings.base_url, wait_until="domcontentloaded", timeout=30000)
        page.locator("#username").fill(settings.username)
        page.locator("#password").fill(settings.password)
        page.locator('input[type="submit"]').click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(10000)
        active_links = page.locator('a[href*="/spec/qt/"]')
        active_count = active_links.count()
        if active_count < 1:
            raise RuntimeError("No existing active test attempt was found")
        href = active_links.nth(0).get_attribute("href")
        page.goto(href, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(10000)
        page.screenshot(path=str(Path(PROBE_DIR) / "active-test.png"), full_page=True)
        card_state = {
            "title": page.title(),
            "url": redact_url(page.url),
            "frames": [snapshot_frame(frame) for frame in page.frames],
        }
        first_question = page.get_by_role("button", name="Перейти к первому вопросу", exact=True)
        first_question_count = first_question.count()
        if first_question_count == 1:
            first_question.click()
        elif " из 80" not in page.locator("body").inner_text(timeout=5000):
            raise RuntimeError("The active attempt exposed neither its card nor a question")
        page.wait_for_timeout(7000)
        page.screenshot(path=str(Path(PROBE_DIR) / "first-question.png"), full_page=True)
        selector_samples = {}
        for selector in (
            ".testQuestion", ".testAnswer", ".testLetter", ".options", ".questionHeader",
            "[class*='question']", "[class*='Question']", "[class*='answer']", "[class*='Answer']",
        ):
            locator = page.locator(selector)
            selector_samples[selector] = {
                "count": locator.count(),
                "texts": [redact(text)[:2000] for text in locator.all_inner_texts()[:30]],
            }
        question_state = {
            "title": page.title(),
            "url": redact_url(page.url),
            "frames": [snapshot_frame(frame) for frame in page.frames],
            "selector_samples": selector_samples,
            "interaction_state": {
                "next": selector_state(page, "button"),
                "answers": selector_state(page, ".testAnswer"),
            },
        }
        page.goto(settings.base_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(10000)
        multicase_link = page.get_by_text("Мультикейс", exact=True)
        multicase_state = None
        if multicase_link.count() == 1:
            multicase_link.click()
            page.wait_for_timeout(7000)
            page.screenshot(path=str(Path(PROBE_DIR) / "multicase.png"), full_page=True)
            multicase_state = {
                "title": page.title(),
                "url": redact_url(page.url),
                "frames": [snapshot_frame(frame) for frame in page.frames],
            }
            active_cases = page.locator('a[href*="/spec/mt/"]')
            if active_cases.count() > 0:
                case_href = active_cases.nth(0).get_attribute("href")
                page.goto(case_href, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(10000)
                page.screenshot(path=str(Path(PROBE_DIR) / "case-attempt.png"), full_page=True)
                multicase_state["case_attempt"] = {
                    "title": page.title(),
                    "url": redact_url(page.url),
                    "frames": [snapshot_frame(frame) for frame in page.frames],
                    "interaction_state": {
                        "inputs": selector_state(page, ".custom-control-input"),
                        "labels": selector_state(page, ".custom-control-label"),
                        "buttons": selector_state(page, "button"),
                    },
                }
                case_first = page.get_by_text("Перейти к первому вопросу", exact=True)
                if case_first.count() == 1:
                    case_first.click()
                    page.wait_for_timeout(7000)
                    page.screenshot(path=str(Path(PROBE_DIR) / "case-first-question.png"), full_page=True)
                    multicase_state["case_question"] = {
                        "title": page.title(),
                        "url": redact_url(page.url),
                        "frames": [snapshot_frame(frame) for frame in page.frames],
                    }
        result = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "title": page.title(),
            "url": redact_url(page.url),
            "multicase_state": multicase_state,
            "card_state": card_state,
            "question_state": question_state,
            "frames": [snapshot_frame(frame) for frame in page.frames],
            "traffic": traffic,
            "browser_messages": messages[-200:],
        }
        (Path(PROBE_DIR) / "active-test.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        browser.close()


if __name__ == "__main__":
    main()
