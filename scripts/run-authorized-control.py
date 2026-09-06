"""Run the explicitly authorized one-test-attempt + one-case control check.

The script requires a long confirmation flag so it cannot be started by
accident. It may create and complete at most one test attempt and one case
attempt for the "Лечебное дело" package.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medik_pilot.exporter import export_json, export_xlsx
from medik_pilot.runner import RunManager
from medik_pilot.storage import Storage


CONFIRMATION = "I_AUTHORIZE_ONE_TEST_ATTEMPT_AND_ONE_CASE"
TERMINAL = {"completed", "partial", "failed", "stopped"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    parser.add_argument(
        "--existing-only",
        action="store_true",
        help="Продолжить только уже открытые попытки и ничего не создавать.",
    )
    parser.add_argument(
        "--material-type",
        choices=("test", "case", "both"),
        default="both",
        help="Ограничить контрольный запуск одним типом материалов.",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Только прочитать уже завершённую попытку, не отправляя ответы.",
    )
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        parser.error("Неверное подтверждение разрешённого контрольного запуска.")

    storage = Storage()
    manager = RunManager(storage)
    run_id = manager.start(
        {
            "source_mode": "live",
            "material_type": args.material_type,
            "specialty": "Лечебное дело",
            # The catalog already contains 80 pilot tests. A target of 160
            # forces exactly one full test attempt to be read and compared.
            "reference_tests": 160,
            "reference_cases": 2 if args.read_only else 1,
            "verification_percent": 0,
            "max_attempts": 1,
            "max_requests": 100,
            "max_duration_minutes": 60,
            "allow_create_attempts": not args.existing_only and not args.read_only,
            "allow_answer_submission": not args.read_only,
            "delay_seconds": 0,
        }
    )
    while True:
        run = storage.get_run(run_id)
        if run and run["status"] in TERMINAL:
            break
        time.sleep(0.5)

    rows = storage.export_rows(run_id)
    result = {
        "status": run["status"],
        "stop_reason": run["stop_reason"],
        "tests_seen": sum(row["kind"] == "test" for row in rows),
        "tests_ready": sum(row["kind"] == "test" and row["status"] == "ready" for row in rows),
        "cases_seen": sum(row["kind"] == "case" for row in rows),
        "cases_ready": sum(row["kind"] == "case" and row["status"] == "ready" for row in rows),
        "errors": run["error_count"],
        "error_message": run["error_message"],
        "json": str(export_json(storage, run_id)),
        "xlsx": str(export_xlsx(storage, run_id)),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if run["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
