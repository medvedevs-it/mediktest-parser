import hashlib
import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CollectedItem:
    kind: str
    source_id: str
    payload: Dict[str, Any]
    raw_payload: Optional[Dict[str, Any]] = None

    @property
    def content_hash(self) -> str:
        normalized = normalize_value(self.payload)
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _PlainTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_entityref(self, name):
        self.parts.append("&" + name + ";")

    def handle_charref(self, name):
        self.parts.append("&#" + name + ";")


def _strip_html(value: str) -> str:
    # A numeric comparison such as '<70 mmHg' is text, not an HTML tag.
    parser = _PlainTextParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def normalize_text(value: str) -> str:
    value = html.unescape(value)
    superscript = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")
    subscript = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
    value = re.sub(
        r"<sup[^>]*>(.*?)</sup>",
        lambda match: _strip_html(match.group(1)).translate(superscript),
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(
        r"<sub[^>]*>(.*?)</sub>",
        lambda match: _strip_html(match.group(1)).translate(subscript),
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = _strip_html(value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_question_text(value: str) -> str:
    text = normalize_text(value)
    # The source often renders questions in ALL CAPS.  The client's import
    # contract requires sentence case while preserving ordinary mixed-case
    # abbreviations, formulas, and drug names already present in the source.
    letters = [character for character in text if character.isalpha()]
    if letters and all(not character.islower() for character in letters):
        text = text.lower()
    for index, character in enumerate(text):
        if character.isalpha():
            return text[:index] + character.upper() + text[index + 1 :]
    return text


_DIAGNOSIS_QUESTION = re.compile(
    r"(?:"
    r"^\s*диагноз\s*$|"
    r"како\w*\s+(?:\w+\s+){0,2}диагноз\w*|"
    r"(?:основн|предварительн|клиническ|заключительн|предполагаем|вероятн)\w*\s+диагноз\w*|"
    r"(?:постав|установ|сформулир|определ)\w*\s+(?:\w+\s+){0,3}диагноз\w*|"
    r"диагноз\w*\s+(?:следует\s+)?(?:постав|установ|сформулир|определ)\w*"
    r")",
    re.IGNORECASE,
)
_DIAGNOSIS_SECTION = re.compile(
    r"(?:^|\b)(?:верный\s+|основной\s+|клинический\s+|заключительный\s+)?диагноз(?:\b|$)",
    re.IGNORECASE,
)
_DIAGNOSIS_PREFIX = re.compile(
    r"^(?:(?:верный|основной|клинический|заключительный)\s+)?диагноз\s*[:\-—]?\s*",
    re.IGNORECASE,
)
_GENERIC_CASE_HEADER = re.compile(r"^ситуационная\s+задача(?:\s+№?\s*\d+)?$", re.IGNORECASE)
_GENERIC_DIAGNOSIS_VALUE = re.compile(
    r"^(?:(?:возможный|предполагаемый|вероятный|верный)\s+)?"
    r"(?:(?:основной|клинический|заключительный)\s+)?диагноз$",
    re.IGNORECASE,
)


def _diagnosis_value(value: Any) -> Optional[str]:
    text = normalize_question_text(str(value or "").replace("**", ""))
    text = _DIAGNOSIS_PREFIX.sub("", text).strip(" .:;-—")
    if not text or _GENERIC_CASE_HEADER.fullmatch(text):
        return None
    if _GENERIC_DIAGNOSIS_VALUE.fullmatch(text):
        return None
    return text


def extract_case_diagnosis(payload: Dict[str, Any]) -> Optional[str]:
    """Return the client-facing case diagnosis for ``exercise_rows.header``.

    Completed attempts usually expose the diagnosis in the diagnosis question
    or in a revealed tab.  Prefer the confirmed correct option, then fall back
    to the revealed section text.  This also lets old bank rows receive a
    proper header during export without rewriting stored source data.
    """
    explicit = _diagnosis_value(payload.get("header"))
    if explicit:
        return explicit

    # On completed attempts the source also renders a ``Диагноз`` section
    # followed by the confirmed answer as the next section.  It contains the
    # fully expanded diagnosis even when the question uses blanks or first
    # asks for a preliminary diagnosis, so it is the primary source.
    sections = list(payload.get("condition_sections") or [])
    for index, section in enumerate(sections):
        title = normalize_text(str(section.get("title") or ""))
        if not _DIAGNOSIS_SECTION.fullmatch(title):
            continue
        candidate = _diagnosis_value(section.get("text"))
        if candidate:
            return candidate
        for block in section.get("content_blocks") or []:
            if str(block.get("type") or "") != "text":
                continue
            candidate = _diagnosis_value(block.get("text"))
            if candidate:
                return candidate
        if index + 1 < len(sections):
            following = sections[index + 1]
            candidate = _diagnosis_value(following.get("title"))
            if candidate:
                return candidate
            candidate = _diagnosis_value(following.get("text"))
            if candidate:
                return candidate

    questions = list(payload.get("questions") or [])
    for question in questions:
        question_text = normalize_text(str(question.get("question") or ""))
        if not _DIAGNOSIS_QUESTION.search(question_text):
            continue
        confirmed = []
        for option in question.get("options") or []:
            if option.get("is_correct") is not True:
                continue
            candidate = _diagnosis_value(option.get("text"))
            if candidate and candidate not in confirmed:
                confirmed.append(candidate)
        if confirmed:
            return "; ".join(confirmed)
    return None


def payload_status(kind: str, payload: Dict[str, Any]) -> str:
    if payload.get("validation_error"):
        return "invalid"
    if kind == "test":
        options = payload.get("options") or []
        if len(options) < 2:
            return "incomplete"
        correctness = [option.get("is_correct") for option in options]
        return "ready" if all(value in {True, False} for value in correctness) and any(correctness) else "missing_key"
    if kind == "case":
        questions = payload.get("questions") or []
        expected = int(payload.get("expected_questions") or len(questions))
        if not payload.get("condition") or len(questions) < expected:
            return "incomplete"
        for question in questions:
            options = question.get("options") or []
            correctness = [option.get("is_correct") for option in options]
            if len(options) < 2:
                return "incomplete"
            if not all(value in {True, False} for value in correctness) or not any(correctness):
                return "missing_key"
        return "ready"
    return "invalid"


def normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: normalize_value(item)
            for key, item in sorted(value.items())
            if key not in {"collected_at", "attempt_id", "position", "error"}
        }
    return value
