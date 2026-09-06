"""Collect five unique completed case variants from the authorized pilot account."""

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from medik_pilot.config import Settings, PROBE_DIR, ensure_directories


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "exports" / "extra-five-cases.json"


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def extract_question(body: str, first_option: str) -> str:
    lines = [line.strip() for line in body.splitlines()]
    try:
        option_index = lines.index(first_option)
    except ValueError:
        return ""
    for index in range(option_index - 1, -1, -1):
        candidate = lines[index]
        if candidate and candidate not in {"Далее", "П", "Д", "Л", "В"} and not candidate.isdigit():
            return norm(candidate)
    return ""


def open_numbered(page, number: int) -> None:
    links = page.locator("a.page-link").filter(has_text=re.compile(r"^\s*{}\s*$".format(number)))
    if links.count() == 0:
        links = page.get_by_text(str(number), exact=True)
    if links.count() == 0:
        raise RuntimeError("Не найдена навигация к вопросу {} кейса".format(number))
    links.last.click(force=True)
    page.locator(".custom-control-label").first.wait_for(state="visible", timeout=30000)
    page.wait_for_timeout(600)


def read_case(page, href: str, variant: str) -> dict:
    page.goto(href, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(20000)
    options = page.locator(".custom-control-label").first
    try:
        options.wait_for(state="visible", timeout=5000)
    except Exception:
        first = page.get_by_role("button", name="Перейти к первому вопросу", exact=True)
        if first.count() != 1:
            first = page.get_by_text("Перейти к первому вопросу", exact=True)
        first.wait_for(state="visible", timeout=30000)
        first.click()
        options.wait_for(state="visible", timeout=30000)

    inputs = page.locator(".custom-control-input")
    if inputs.count() and not inputs.first.is_disabled():
        # Answer all questions with the first option solely to unlock the
        # result view; keys are read only from the resulting review page.
        for number in range(1, 13):
            labels = page.locator(".custom-control-label")
            labels.first.click(force=True)
            next_button = page.locator("#next")
            if next_button.count() != 1 or not next_button.is_visible():
                next_button = page.get_by_role("button", name="Далее", exact=True)
            try:
                next_button.wait_for(state="visible", timeout=30000)
            except Exception as exc:
                raise RuntimeError("Кнопка перехода не появилась после вопроса {}: {}".format(number, page.locator("body").inner_text()[-1200:])) from exc
            next_button.click(force=True)
            page.wait_for_timeout(1800 if number < 12 else 10000)

    result_button = page.get_by_role("button", name="Результат", exact=True)
    if result_button.count() == 1:
        result_button.click(force=True)
        page.wait_for_timeout(3000)

    sections = page.evaluate(
        """() => Array.from(document.querySelectorAll('a.nav-link[href^=\"#i\"]')).map(link => {
          const id = (link.getAttribute('href') || '').slice(1);
          const target = id ? document.getElementById(id) : null;
          return {title: (link.innerText || '').trim(), text: target ? (target.innerText || '').trim() : ''};
        }).filter(item => item.title && item.text)"""
    )
    sections = [{"title": norm(row["title"]), "text": norm(row["text"])} for row in sections]
    questions = []
    for number in range(1, 13):
        if number > 1:
            open_numbered(page, number)
        body = page.locator("body").inner_text()
        labels = page.locator(".custom-control-label")
        values = [norm(value) for value in labels.all_inner_texts() if norm(value)]
        if len(values) != 4:
            raise RuntimeError("В кейсе {} у вопроса {} ожидалось 4 варианта".format(variant, number))
        options = []
        for index in range(labels.count()):
            label = labels.nth(index)
            parent_class = str(label.locator("..").get_attribute("class") or "")
            options.append({
                "text": values[index],
                "is_correct": "text-success" in parent_class or "correct_answer" in parent_class,
            })
        if sum(option["is_correct"] for option in options) != 1:
            raise RuntimeError("В кейсе {} у вопроса {} не найден ровно один ключ".format(variant, number))
        questions.append({
            "question_number": number,
            "question": extract_question(body, values[0]),
            "options": options,
        })
    return {"variant": variant, "condition_sections": sections, "questions": questions}


def main() -> None:
    settings = Settings.from_env()
    ensure_directories()
    cases = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        def login() -> None:
            page.goto(settings.base_url, wait_until="domcontentloaded", timeout=30000)
            page.locator("#username").fill(settings.username)
            page.locator("#password").fill(settings.password)
            page.locator('input[type="submit"]').click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(15000)

        def case_links():
            page.locator("text=Мультикейс").first.wait_for(state="visible", timeout=30000)
            page.locator("text=Мультикейс").first.click()
            page.wait_for_timeout(10000)
            return [
                (link.get_attribute("href"), parse_qs(urlparse(link.get_attribute("href")).query).get("variant", [""])[0])
                for link in page.locator('a[href*="/spec/mt/"]').all()
            ]

        login()
        for href, variant in case_links():
            if variant and variant not in cases:
                cases[variant] = read_case(page, href, variant)
                if len(cases) >= 5:
                    break

        attempts = 0
        while len(cases) < 5 and attempts < 8:
            attempts += 1
            page.goto(settings.base_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(12000)
            page.locator("text=Мультикейс").first.click()
            page.wait_for_timeout(10000)
            start = page.get_by_role("button", name="Решать кейс", exact=True)
            start.wait_for(state="visible", timeout=30000)
            start.click()
            page.wait_for_timeout(6000)
            package = page.get_by_text("Лечебное дело, 2026", exact=True)
            package.wait_for(state="visible", timeout=30000)
            package.click()
            page.wait_for_timeout(10000)
            href = page.locator('a[href*="/spec/mt/"]').first.get_attribute("href")
            variant = parse_qs(urlparse(href).query).get("variant", [""])[0]
            if variant and variant not in cases:
                cases[variant] = read_case(page, href, variant)
            time.sleep(1)
        browser.close()

    if len(cases) < 5:
        raise RuntimeError("Удалось получить только {} уникальных кейса из 5".format(len(cases)))
    document = {"specialty": "Лечебное дело", "cases": list(cases.values())[:5]}
    OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "cases": len(document["cases"]), "questions": 12}, ensure_ascii=False))


if __name__ == "__main__":
    main()
