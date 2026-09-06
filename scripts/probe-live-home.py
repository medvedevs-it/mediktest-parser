"""Read-only diagnostic of the trainer home page after login."""

import argparse
import base64
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("credentials_base64")
    parser.add_argument("--output", default=r"C:\MedikTestData\probes\live-home.png")
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
        page.wait_for_timeout(15000)
        page.goto(args.base_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(10000)
        buttons = [clean(text) for text in page.locator("button").all_inner_texts() if clean(text)]
        links = page.locator("a").evaluate_all(
            """nodes => nodes.map(node => ({
                text: (node.innerText || '').replace(/\\s+/g, ' ').trim(),
                href: node.getAttribute('href') || ''
            })).filter(item => item.text || item.href)"""
        )
        body = clean(page.locator("body").inner_text())
        page.screenshot(path=str(target), full_page=True)
        print(
            json.dumps(
                {
                    "url": page.url,
                    "title": page.title(),
                    "buttons": buttons,
                    "links": links,
                    "body_excerpt": body[:4000],
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
