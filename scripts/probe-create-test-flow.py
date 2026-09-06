"""Authorized one-shot probe of the current test-attempt creation flow."""

import argparse
import base64
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def snapshot(page, label: str) -> dict:
    return {
        "label": label,
        "url": page.url,
        "buttons": [
            clean(text) for text in page.locator("button").all_inner_texts() if clean(text)
        ],
        "links": page.locator("a").evaluate_all(
            """nodes => nodes.map(node => ({
                text: (node.innerText || '').replace(/\\s+/g, ' ').trim(),
                href: node.getAttribute('href') || ''
            })).filter(item => item.text || item.href)"""
        ),
        "body_excerpt": clean(page.locator("body").inner_text())[:3500],
    }


def first_visible(locator, timeout_seconds: int = 15):
    deadline = time.monotonic() + timeout_seconds
    while True:
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                return candidate
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("credentials_base64")
    parser.add_argument(
        "--output",
        default=r"C:\MedikTestData\probes\test-attempt-created.png",
    )
    parser.add_argument("--base-url", default="https://selftest.mededtech.ru/")
    args = parser.parse_args()
    credentials = json.loads(base64.b64decode(args.credentials_base64).decode("utf-8"))
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.goto(args.base_url, wait_until="domcontentloaded", timeout=30000)
        page.locator("#username").fill(credentials["username"])
        page.locator("#password").fill(credentials["password"])
        page.locator('input[type="submit"]').click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)
        page.goto(args.base_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)

        start = first_visible(page.get_by_text("Пройти тестирование", exact=True))
        if start is None:
            raise RuntimeError("Visible «Пройти тестирование» control not found.")
        start.click()
        page.wait_for_timeout(3000)
        before_package = snapshot(page, "before_package")
        before_image = target.with_name(target.stem + "-before-package.png")
        before_json = target.with_name(target.stem + "-before-package.json")
        page.screenshot(path=str(before_image), full_page=True)
        before_json.write_text(
            json.dumps(before_package, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(before_package, ensure_ascii=True), flush=True)

        package = first_visible(
            page.get_by_role(
                "button",
                name=re.compile(r"Лечебное дело.*2026", re.I),
            )
        )
        if package is None:
            raise RuntimeError("Visible «Лечебное дело, 2026» package not found.")
        package.click()
        page.wait_for_timeout(7000)
        after_creation = snapshot(page, "after_creation")
        page.screenshot(path=str(target), full_page=True)
        print(
            json.dumps(
                {
                    "before_package": before_package,
                    "after_creation": after_creation,
                    "screenshot": str(target),
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
