"""Sequential quiz adapter for the API used by the REH2 web client.

No password/token is written to disk. Creation is deliberately NOT retried:
after an uncertain response only history reconciliation is allowed.
"""
import hashlib
import json
import re
import socket
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from ..domain import CollectedItem, normalize_question_text, normalize_text
from ..specialties import normalize_specialty, PACKAGE_TITLES
from .live import CollectionConfigurationError


class Reh2Error(CollectionConfigurationError):
    """Permanent/actionable failure. Never retry as a generic browser error."""


class Reh2SourceExhausted(Reh2Error):
    def __init__(self):
        super().__init__(
            "REH2: вопросы в последовательной истории этого аккаунта закончились. "
            "Полученные материалы сохранены, история не сброшена. "
            "Это не подтверждает полноту банка текущего запуска."
        )


class Reh2TransientError(Exception):
    pass


class CollectionInterrupted(Exception):
    pass


GROUPS = {"Лечебное дело": "GeneralMedicine_2026", "Педиатрия": "PediatricsSpec_2026"}
CODES = {"Лечебное дело": "31.05.01", "Педиатрия": "31.05.02"}


def checked_uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise Reh2Error("REH2: отсутствует или изменился UUID в ответе сайта.") from None


def verify_bank(bank, specialty):
    if not isinstance(bank, dict) or (
        bank.get("packageName") != PACKAGE_TITLES[specialty]
        or bank.get("packageIdGroup") != GROUPS[specialty]
        or (bank.get("speciality") or {}).get("code") != CODES[specialty]
        or not re.fullmatch(re.escape(GROUPS[specialty]) + r"_v\d+", bank.get("packageId", ""))
    ):
        raise Reh2Error("REH2: пакет не соответствует специальности «{}» и 2026 году.".format(specialty))
    checked_uuid(bank.get("uid"))


class Reh2Api:
    BASE = "https://reh2-test.mededtech.ru/reh2-api"

    def __init__(self, username, password, timeout=30):
        self.username, self.password = username, password
        self.token = None
        self.timeout = timeout

    def request(self, path, body=None, authenticate=True):
        if authenticate and not self.token:
            token = self.request("/auth/login", {"login": self.username, "password": self.password}, False)
            self.token = checked_uuid(token)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}
        if authenticate:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            self.BASE + path, headers=headers, method="POST",
            data=json.dumps(body).encode("utf-8") if body is not None else b"",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 409 and path == "/quiz/start-attempt":
                # Only this verified response proves exhaustion; other conflicts
                # must remain errors. Never log arbitrary response bodies.
                try:
                    detail = json.loads(exc.read(8192))
                except (ValueError, UnicodeError):
                    detail = None
                if isinstance(detail, dict) and detail.get("message") == (
                    "Закончились вопросы по данной дисциплине. Необходимо очистить историю"
                ):
                    raise Reh2SourceExhausted() from None
            if exc.code in (401, 403):
                self.token = None
                raise Reh2Error("REH2: вход отклонён или сессия истекла. Повторно укажите данные аккаунта.") from None
            if exc.code in (408, 429, 500, 502, 503, 504):
                raise Reh2TransientError("REH2: временная ошибка HTTP {}.".format(exc.code)) from None
            raise Reh2Error("REH2: запрос отклонён (HTTP {}). Требуется проверка API; повтор отключён.".format(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
            raise Reh2TransientError("REH2: сеть недоступна или превышено время ожидания.") from None
        if not content:
            return None
        try:
            return json.loads(content)
        except ValueError:
            # Login and creation return a plain UUID, not a JSON string.
            return checked_uuid(content.strip())

    def banks(self):
        result = self.request("/quiz/bank-grid-data", {
            "packageName": None, "gridStateUi": {"limit": 0, "offset": 0},
            "takeIntoUserSpecialities": True,
        })
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise Reh2Error("REH2: изменился формат списка пакетов.")
        return result["items"]

    def history(self):
        # Read every page: a limited history cannot prove absence of an attempt.
        rows, offset = [], 0
        while True:
            result = self.request("/quiz/attempt-grid-data", {
                "bank": None, "dateFrom": "", "dateTo": "", "sort": "started-desc",
                "gridStateUi": {"limit": 100, "offset": offset},
            })
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise Reh2Error("REH2: изменился формат истории попыток.")
            page = result["items"]
            if any(not isinstance(row, dict) or not row.get("uid") for row in page):
                raise Reh2Error("REH2: история содержит попытку без идентификатора.")
            rows.extend(page)
            if len(rows) >= int(result["totalCount"]):
                if len({r["uid"] for r in rows}) != len(rows):
                    raise Reh2Error("REH2: история изменилась во время чтения; повторите проверку.")
                return rows
            if not page:
                raise Reh2Error("REH2: не удалось прочитать историю полностью.")
            offset += len(page)

    def create(self, bank):
        return checked_uuid(self.request("/quiz/start-attempt", {
            "quizBank": bank, "continuous": True, "mustClearHistory": False,
            "answerPreviewAllowed": True, "errors": {},
        }))

    def attempt(self, uid):
        return self.request("/quiz/attempt/" + checked_uuid(uid))

    def finish(self, uid):
        return self.request("/quiz/finish-attempt/" + checked_uuid(uid))

    def report(self, uid):
        return self.request("/quiz/attempt/{}/statistics?mode=FULL".format(checked_uuid(uid)))


def parse_report(report, specialty, package, attempt_uid, operation, expected=None):
    """Use structured questions embedded in the final report, not image colours.

    Each visible question gets exactly one row, including malformed questions.
    An unrecognisable report is a permanent failure, never an empty success.
    """
    if not isinstance(report, dict) or report.get("uid") != attempt_uid or report.get("archived"):
        raise Reh2Error("REH2: отчёт другой попытки либо перенесён в архив.")
    html = report.get("statistics", "")
    if "<!DOCTYPE" in html.upper() or "<!ENTITY" in html.upper():
        raise Reh2Error("REH2: неподдерживаемая структура отчёта.")
    try:
        root = ET.fromstring(html)
    except (ET.ParseError, TypeError):
        raise Reh2Error("REH2: изменился формат итогового отчёта.") from None
    body = root.find("body")
    if body is None:
        raise Reh2Error("REH2: в отчёте отсутствует содержимое.")
    paragraphs = ["".join(p.itertext()) for p in body.findall("p")]
    if "Банк тестовых заданий: " + PACKAGE_TITLES[specialty] not in paragraphs:
        raise Reh2Error("REH2: специальность отчёта не совпадает с выбранной.")
    blocks = []
    for node in body:
        if node.tag == "h3" and "".join(node.itertext()).startswith("Вопрос "):
            blocks.append([])
        elif blocks:
            blocks[-1].append(node)
    if not blocks or (expected is not None and len(blocks) != expected):
        raise Reh2Error("REH2: число вопросов отчёта не совпадает с выданным пакетом.")
    items, uids = [], set()
    for index, block in enumerate(blocks):
        raw, error = {}, None
        try:
            data_nodes = [n for n in block if n.get("class") == "question-uid"]
            if len(data_nodes) != 1:
                raise ValueError("нет однозначных данных вопроса")
            raw = json.loads("".join(data_nodes[0].itertext()))
            uid = checked_uuid(raw.get("uid"))
            if uid in uids:
                raise ValueError("повтор UUID внутри пакета")
            uids.add(uid)
            options = raw.get("answers")
            if not isinstance(options, list) or len(options) != 4:
                raise ValueError("должно быть четыре варианта")
            if any(type(a.get("correct")) is not bool for a in options) or sum(a["correct"] for a in options) != 1:
                raise ValueError("нет единственного подтверждённого правильного ответа")
            payload = {
                "question": normalize_question_text(raw.get("questionText") or ""),
                "options": [{"text": normalize_text(a.get("text") or ""), "is_correct": a["correct"]} for a in options],
            }
            if not payload["question"] or any(not a["text"] for a in payload["options"]):
                raise ValueError("пустой вопрос или ответ")
            if len({a["text"].casefold() for a in payload["options"]}) != 4:
                raise ValueError("повторяющиеся варианты ответа")
            if re.search(r"<\s*img\b", str(raw), re.I):
                raise ValueError("изображение в тесте требует проверки экспорта")
        except (ValueError, TypeError, KeyError, AttributeError, Reh2Error) as exc:
            error = str(exc)
            uid = "invalid-{}-{}".format(attempt_uid, index)
            payload = {"question": "", "options": [], "validation_error": error}
        metadata = {"provider": "reh2", "specialty": specialty, "package": package,
                    "question_uid": uid, "attempt_uid": attempt_uid,
                    "operation": operation, "index": index, "error": error}
        items.append(CollectedItem("test", "reh2-" + uid, payload,
                                   {"question": raw, "_reh2": metadata}))
    return items


class Reh2Collector:
    supports_safe_resume = True
    whole_test_packages = True

    def __init__(self, settings, specialty, storage, run_id, legacy,
                 allow_create_attempts=False, allow_answer_submission=False, api=None):
        self.specialty = normalize_specialty(specialty)
        self.storage, self.run_id, self.legacy = storage, run_id, legacy
        self.allow_create_attempts = allow_create_attempts
        self.allow_answer_submission = allow_answer_submission
        self.api = api or Reh2Api(settings.username, settings.password)
        self.account = hashlib.sha256(settings.username.strip().casefold().encode()).hexdigest()
        self.stop_callback = lambda: False
        self.pause_callback = lambda: None

    def checkpoint(self):
        self.pause_callback()
        if self.stop_callback():
            raise CollectionInterrupted()

    def collect_attempt(self, kind, number):
        if kind != "test":
            self.legacy.pause_callback = self.pause_callback
            return self.legacy.collect_attempt(kind, number)
        self.checkpoint()
        state = self.storage.reh2_state(self.run_id)
        if state and state["account"] != self.account:
            raise Reh2Error("REH2: аккаунт изменился. Продолжите прежним аккаунтом или создайте отдельный запуск.")
        if state and state["phase"] == "exhausted":
            raise Reh2SourceExhausted()
        no_new_packages = (state or {}).get("no_new_packages", 0)
        if state and state["phase"] == "committed" and state["operation"] != number:
            state = None
        if not state:
            if not self.allow_create_attempts:
                raise Reh2Error("REH2: разрешите создание последовательных попыток.")
            if not self.allow_answer_submission:
                raise Reh2Error("REH2: разрешите завершение попытки для получения итогового отчёта.")
            banks = [b for b in self.api.banks() if b.get("packageName") == PACKAGE_TITLES[self.specialty]]
            for b in banks:
                verify_bank(b, self.specialty)
            if not banks:
                raise Reh2Error("REH2: недоступен пакет «{}».".format(PACKAGE_TITLES[self.specialty]))
            bank = max(banks, key=lambda b: int(b["version"]))
            if sum(b["version"] == bank["version"] for b in banks) != 1:
                raise Reh2Error("REH2: найдено несколько версий одного пакета с одинаковым номером.")
            history = self.api.history()
            if any(not h.get("timeFinished") and h.get("bank", {}).get("uid") == bank["uid"] for h in history):
                raise Reh2Error("REH2: уже есть незавершённая внешняя попытка без контрольной точки. Завершите её на сайте.")
            state = {"account": self.account, "specialty": self.specialty, "package": bank["packageId"],
                     "bank": bank, "operation": number, "phase": "creating",
                     "ready_before": len(self.storage.run_ready_identity_keys(self.run_id)["test"]),
                     "no_new_packages": no_new_packages,
                     "history_before": [h["uid"] for h in history]}
            self.checkpoint()
            self.storage.save_reh2_state(self.run_id, state)
            # Once 'creating' is durable, only this invocation may call create.
            try:
                state["attempt_uid"] = self.api.create(bank)
            except Reh2SourceExhausted:
                state["phase"] = "exhausted"
                self.storage.save_reh2_state(self.run_id, state)
                raise
            state["phase"] = "created"
            self.storage.save_reh2_state(self.run_id, state)
        if state["phase"] == "creating":
            candidates = [h for h in self.api.history() if h["uid"] not in state["history_before"]
                          and h.get("bank", {}).get("uid") == state["bank"]["uid"]]
            if len(candidates) != 1:
                raise Reh2Error("REH2: результат создания попытки неоднозначен (найдено {}). Новая попытка не создана; проверьте историю.".format(len(candidates)))
            state.update(attempt_uid=candidates[0]["uid"], phase="created")
            self.storage.save_reh2_state(self.run_id, state)
        uid = state["attempt_uid"]
        if state["phase"] in ("created", "finishing"):
            self.checkpoint()
            attempt = self.api.attempt(uid)
            if not isinstance(attempt, dict) or attempt.get("uid") != uid:
                raise Reh2Error("REH2: получена другая попытка.")
            verify_bank(attempt.get("bank"), self.specialty)
            if attempt["bank"]["uid"] != state["bank"]["uid"]:
                raise Reh2Error("REH2: пакет попытки изменился.")
            if not attempt.get("timeFinished"):
                questions = attempt.get("questions")
                if not isinstance(questions, list) or not questions:
                    raise Reh2Error("REH2: активная попытка не содержит списка вопросов.")
                state.update(expected=len(questions), phase="finishing")
                self.storage.save_reh2_state(self.run_id, state)
                self.checkpoint()
                self.api.finish(uid)
            state["phase"] = "reading"
            self.storage.save_reh2_state(self.run_id, state)
        if state["phase"] == "reading":
            self.checkpoint()
            report = self.api.report(uid)
            # Save the complete original response before parsing/importing any row.
            self.storage.save_reh2_report(self.run_id, state["operation"], report)
            state["phase"] = "importing"
            self.storage.save_reh2_state(self.run_id, state)
        report = self.storage.reh2_report(self.run_id, state["operation"])
        items = parse_report(report, self.specialty, state["package"], uid, state["operation"], state.get("expected"))
        state["package_count"] = len(items)
        self.storage.save_reh2_state(self.run_id, state)
        return items

    def acknowledge_attempt(self, kind):
        if kind == "test":
            return self.storage.finish_reh2_package(self.run_id)

    def reset_session(self):
        # Preserve the API token and durable operation. Only reset legacy browser.
        self.legacy.reset_session()

    def close(self):
        self.api.token = None
        self.legacy.close()
