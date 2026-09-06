"""Authorized probe of a case question before and after selecting one option."""

import argparse
import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def first_visible(locator, timeout_seconds: int = 15):
    deadline = time.monotonic() + timeout_seconds
    while True:
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                return candidate
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def locator_state(locator) -> list[dict]:
    result = []
    for index in range(locator.count()):
        candidate = locator.nth(index)
        result.append(
            {
                "index": index,
                "visible": candidate.is_visible(),
                "enabled": candidate.is_enabled(),
                "html": candidate.evaluate("(node) => node.outerHTML"),
            }
        )
    return result


def snapshot(page, label: str) -> dict:
    return {
        "label": label,
        "url": page.url,
        "options": [
            clean(value)
            for value in page.locator(".custom-control-label").all_inner_texts()
            if clean(value)
        ],
        "inputs": page.locator(".custom-control-input").evaluate_all(
            """nodes => nodes.map(node => ({
                id: node.id || '',
                type: node.type || '',
                checked: Boolean(node.checked),
                disabled: Boolean(node.disabled)
            }))"""
        ),
        "next_by_id": locator_state(page.locator("#next")),
        "next_by_text": locator_state(page.get_by_text("Далее", exact=True)),
        "buttons": [
            clean(text) for text in page.locator("button").all_inner_texts() if clean(text)
        ],
        "body_excerpt": clean(page.locator("body").inner_text())[-4000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("credentials_base64")
    parser.add_argument(
        "--output-dir",
        default=r"C:\MedikTestData\probes",
    )
    parser.add_argument("--variant", default="")
    parser.add_argument("--question", type=int, default=0)
    parser.add_argument("--base-url", default="https://selftest.mededtech.ru/")
    args = parser.parse_args()
    credentials = json.loads(base64.b64decode(args.credentials_base64).decode("utf-8"))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

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

        navigation = first_visible(page.get_by_text("Мультикейс", exact=True))
        if navigation is None:
            raise RuntimeError("Visible «Мультикейс» navigation not found.")
        navigation.click()
        page.wait_for_timeout(5000)
        links = []
        for link in page.locator('a[href*="/spec/mt/"]').all():
            href = link.get_attribute("href") or ""
            variant = parse_qs(urlparse(href).query).get("variant", [""])[0]
            links.append({"href": href, "variant": variant})
        if not links:
            raise RuntimeError("No case links found.")

        selected_link = next(
            (link for link in links if link["variant"] == args.variant),
            links[0],
        )
        page.goto(selected_link["href"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(6000)
        if not first_visible(page.locator(".custom-control-label"), timeout_seconds=3):
            first = first_visible(
                page.get_by_text("Перейти к первому вопросу", exact=True)
            )
            if first is None:
                raise RuntimeError("Cannot open the first case question.")
            first.click()
            page.wait_for_timeout(5000)
        if args.question:
            question = first_visible(
                page.get_by_text(str(args.question), exact=True)
            )
            if question is None:
                raise RuntimeError(
                    "Question navigation {} not found.".format(args.question)
                )
            question.click(force=True)
            page.wait_for_timeout(3000)

        before = snapshot(page, "before_selection")
        page.screenshot(
            path=str(output_dir / "case-before-selection.png"),
            full_page=True,
        )
        labels = page.locator(".custom-control-label")
        if labels.count() < 1:
            raise RuntimeError("No answer option found.")
        labels.first.click(force=True)
        page.wait_for_timeout(2000)
        after = snapshot(page, "after_selection")
        page.screenshot(
            path=str(output_dir / "case-after-selection.png"),
            full_page=True,
        )
        print(
            json.dumps(
                {"links": links, "before": before, "after": after},
                ensure_ascii=True,
                indent=2,
            )
        )
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
