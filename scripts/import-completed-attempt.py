"""Import the locally captured 80-question completed attempt into the pilot database."""

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medik_pilot.domain import CollectedItem
from medik_pilot.exporter import export_json, export_xlsx
from medik_pilot.storage import Storage, utc_now


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "exports" / "special-attempt-precompletion.json"


def source_id(question: dict) -> str:
    answers = [option.get("text", "") for option in question.get("options", [])]
    identity = hashlib.sha256(
        (question.get("question", "") + "\n" + "\n".join(answers)).encode("utf-8")
    ).hexdigest()[:24]
    return "live-test-{}".format(identity)


def main() -> None:
    document = json.loads(SOURCE.read_text(encoding="utf-8"))
    questions = document.get("questions", [])
    numbers = [question.get("question_number") for question in questions]
    if len(questions) != 80 or numbers != list(range(1, 81)):
        raise RuntimeError("Ожидались все вопросы с номерами 1–80 без пропусков.")

    run_id = "completed-attempt-{}".format(uuid.uuid4())
    storage = Storage()
    storage.create_run(
        run_id,
        {
            "source_mode": "live",
            "material_type": "test",
            "specialty": "Лечебное дело",
            "max_attempts": 1,
            "max_duration_minutes": 60,
            "saturation_window": 1,
            "novelty_threshold": 0.0,
            "delay_seconds": 0,
        },
    )
    now = utc_now()
    storage.update_run(run_id, status="running", started_at=now)
    counts = {"new": 0, "duplicate": 0, "changed": 0}
    for question in questions:
        payload = {
            "specialty": "Лечебное дело",
            "question": question["question"],
            "question_number": question["question_number"],
            "question_total": question["question_total"],
            "options": question["options"],
            "correct_answer_status": "not_disclosed_for_unanswered_completed_attempt",
            "attempt_completed": True,
            "attempt_result": "0/80",
            "synthetic": False,
        }
        outcome, _ = storage.store_item(
            run_id,
            "live",
            "Лечебное дело",
            CollectedItem("test", source_id(question), payload),
            1,
        )
        counts[outcome] += 1

    storage.add_attempt_stats(run_id, 1, "test", counts)
    storage.add_event(
        run_id,
        "Попытка завершена без ответов; тренажёр не раскрыл правильные варианты для пропущенных вопросов.",
        "warning",
    )
    storage.update_run(
        run_id,
        status="completed",
        stop_reason="special_attempt_completed",
        finished_at=utc_now(),
        attempts_completed=1,
        items_seen=len(questions),
        unique_items=counts["new"],
        duplicate_items=counts["duplicate"],
        changed_items=counts["changed"],
    )
    targets = [export_json(storage, run_id), export_xlsx(storage, run_id)]
    for target in targets:
        os.chmod(target, 0o600)
    print(run_id)
    for target in targets:
        print(target)


if __name__ == "__main__":
    main()
