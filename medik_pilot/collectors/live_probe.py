import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from ..config import PROBE_DIR, Settings, ensure_directories
from ..domain import CollectedItem


class LiveProbeCollector:
    """Authorized diagnostic adapter. Collection selectors are added after the first probe."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._probed = False

    def collect_attempt(self, kind: str, attempt: int) -> List[CollectedItem]:
        if self._probed:
            raise RuntimeError("Живой профиль тренажёра ещё не настроен по результатам диагностического входа.")
        self._probed = True
        self._run_probe()
        raise RuntimeError(
            "Авторизованный диагностический вход выполнен. Снимок сохранён в data/probes; "
            "следующий этап — привязать внутренние запросы и селекторы попытки."
        )

    def _run_probe(self) -> None:
        if not self.settings.username or not self.settings.password:
            raise RuntimeError("Заполните SELFTEST_USERNAME и SELFTEST_PASSWORD в файле .env.")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright не установлен. Запустите scripts/setup-windows.ps1.") from exc

        ensure_directories()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.settings.headless)
            page = browser.new_page()
            endpoints = []
            browser_messages = []

            page.on(
                "console",
                lambda message: browser_messages.append({
                    "type": "console:{}".format(message.type),
                    "text": self._redact(message.text)[:2000],
                }),
            )
            page.on(
                "pageerror",
                lambda error: browser_messages.append({
                    "type": "pageerror",
                    "text": self._redact(str(error))[:4000],
                }),
            )

            def record_response(response):
                request = response.request
                if request.resource_type not in {"document", "xhr", "fetch"}:
                    return
                content_type = response.headers.get("content-type", "")
                entry = {
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "status": response.status,
                    "url": response.url,
                    "content_type": content_type.split(";", 1)[0],
                }
                if response.url.endswith("/secured/data"):
                    post_data = request.post_data or ""
                    try:
                        body_preview = response.text()[:12000]
                    except Exception:
                        body_preview = "[unreadable]"
                    entry["request_preview"] = self._redact(post_data[:6000])
                    entry["response_preview"] = self._redact(body_preview)
                if "json" in content_type:
                    try:
                        data = response.json()
                        if isinstance(data, dict):
                            entry["json_shape"] = sorted(data.keys())[:50]
                        elif isinstance(data, list):
                            entry["json_shape"] = "list[{}]".format(len(data))
                    except Exception:
                        entry["json_shape"] = "unreadable"
                endpoints.append(entry)

            page.on("response", record_response)
            page.goto(self.settings.base_url, wait_until="domcontentloaded", timeout=30000)
            page.locator("#username").fill(self.settings.username)
            page.locator("#password").fill(self.settings.password)
            page.locator('input[type="submit"]').click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            if "login" in page.url.lower():
                browser.close()
                raise RuntimeError("Тренажёр не принял учётные данные или потребовал дополнительное действие.")
            page.wait_for_timeout(10000)
            page.screenshot(path=str(Path(PROBE_DIR) / "authorized-ui.png"), full_page=True)
            page_text = page.locator("body").inner_text(timeout=10000)
            page_text = self._redact(page_text)
            controls = page.locator("button, a, input, select, [role='button'], [onclick], .x-btn").evaluate_all(
                """elements => elements.slice(0, 200).map(element => ({
                    tag: element.tagName,
                    text: (element.innerText || element.value || '').trim().slice(0, 300),
                    id: element.id || null,
                    class_name: String(element.className || '').slice(0, 300),
                    role: element.getAttribute('role'),
                    type: element.getAttribute('type'),
                    href: element.getAttribute('href')
                }))"""
            )
            frames = []
            for frame in page.frames:
                try:
                    frame_text = self._redact(frame.locator("body").inner_text(timeout=5000))
                    frame_controls = frame.locator(
                        "button, a, input, select, [role='button'], [onclick], .x-btn"
                    ).evaluate_all(
                        """elements => elements.slice(0, 300).map(element => ({
                            tag: element.tagName,
                            text: (element.innerText || element.value || '').trim().slice(0, 300),
                            id: element.id || null,
                            class_name: String(element.className || '').slice(0, 300),
                            role: element.getAttribute('role'),
                            type: element.getAttribute('type'),
                            href: element.getAttribute('href')
                        }))"""
                    )
                except Exception as exc:
                    frame_text = "[frame unreadable: {}]".format(type(exc).__name__)
                    frame_controls = []
                frames.append({
                    "name": frame.name,
                    "url": frame.url,
                    "visible_text": frame_text[:30000],
                    "controls": frame_controls,
                })
            snapshot = {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "url": page.url,
                "title": page.title(),
                "visible_text": page_text[:20000],
                "controls": controls,
                "frames": frames,
                "endpoints": endpoints,
                "browser_messages": browser_messages[-100:],
            }
            target = Path(PROBE_DIR) / "authorized-ui.json"
            target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()

    def _redact(self, value: str) -> str:
        value = value.replace(self.settings.username, "[account]")
        value = value.replace(self.settings.password, "[secret]")
        return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}", "[email]", value)
