from typing import Dict, List

from ..domain import CollectedItem
from ..specialties import DEFAULT_SPECIALTY, normalize_specialty


TEST_TOPICS = [
    "первичная диагностика", "тактика обследования", "интерпретация анализа",
    "неотложная помощь", "фармакотерапия", "профилактика", "диспансеризация",
    "дифференциальная диагностика", "маршрутизация пациента", "контроль лечения",
    "оценка факторов риска", "медицинская документация", "клиническое наблюдение",
    "подготовка к исследованию", "оценка жалоб", "сбор анамнеза",
]


class DemoCollector:
    """Synthetic source that exercises the full pipeline without copying FMZA content."""

    def __init__(self, specialty: str = DEFAULT_SPECIALTY):
        self.specialty = normalize_specialty(specialty)

    def collect_attempt(self, kind: str, attempt: int) -> List[CollectedItem]:
        if kind == "test":
            return self._tests(attempt)
        if kind == "case":
            return self._cases(attempt)
        raise ValueError("Unknown material kind: {}".format(kind))

    def _tests(self, attempt: int) -> List[CollectedItem]:
        start = ((attempt - 1) * 5) % len(TEST_TOPICS)
        indexes = [(start + offset) % len(TEST_TOPICS) for offset in range(10)]
        return [self._test_item(index) for index in indexes]

    def _test_item(self, index: int) -> CollectedItem:
        topic = TEST_TOPICS[index]
        payload: Dict = {
            "specialty": self.specialty,
            "question": "Демонстрационный вопрос: выберите верное действие для темы «{}».".format(topic),
            "single_answer": True,
            "options": [
                {"text": "Действие A", "is_correct": index % 4 == 0},
                {"text": "Действие B", "is_correct": index % 4 == 1},
                {"text": "Действие C", "is_correct": index % 4 == 2},
                {"text": "Действие D", "is_correct": index % 4 == 3},
            ],
            "synthetic": True,
        }
        return CollectedItem(kind="test", source_id="demo-test-{:03d}".format(index + 1), payload=payload)

    def _cases(self, attempt: int) -> List[CollectedItem]:
        indexes = [((attempt - 1) + offset) % 4 for offset in range(2)]
        return [self._case_item(index) for index in indexes]

    def _case_item(self, index: int) -> CollectedItem:
        questions = []
        sections = ["План обследования", "Диагноз", "Лечение"]
        for number in range(1, 5):
            correct = (index + number) % 3
            questions.append(
                {
                    "section": sections[(number - 1) % len(sections)],
                    "question": "Демонстрационное задание {} для случая {}".format(number, index + 1),
                    "multiple": False,
                    "options": [
                        {"text": "Вариант {}.{}".format(number, option + 1), "is_correct": option == correct}
                        for option in range(3)
                    ],
                }
            )
        payload = {
            "specialty": self.specialty,
            "header": "Демонстрационный диагноз {}".format(index + 1),
            "condition": "Демонстрационное условие клинического случая №{} без реальных данных пациента.".format(index + 1),
            "information_blocks": ["Исходные сведения", "Результаты условного обследования"],
            "questions": questions,
            "synthetic": True,
        }
        return CollectedItem(kind="case", source_id="demo-case-{:03d}".format(index + 1), payload=payload)
