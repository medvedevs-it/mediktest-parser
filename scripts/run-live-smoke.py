"""Run one small, read-only live collection through the service layer.

This diagnostic script never creates or completes attempts. Any state-changing
verification must be started explicitly from the UI for one approved run.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medik_pilot.runner import RunManager
from medik_pilot.storage import Storage


def main() -> None:
    storage = Storage()
    manager = RunManager(storage)
    config = {
        "source_mode": "live",
        "material_type": "both",
        "specialty": "Лечебное дело",
        "reference_tests": 3,
        "reference_cases": 1,
        "verification_percent": 0,
        "max_attempts": 1,
        "max_requests": 4,
        "max_duration_minutes": 10,
        "allow_create_attempts": False,
        "allow_answer_submission": False,
        "delay_seconds": 0,
    }
    run_id = manager.start(config)
    while True:
        run = storage.get_run(run_id)
        if run and run["status"] in {"completed", "partial", "failed", "stopped"}:
            print(
                "run={} status={} seen={} new={} duplicates={} error={}".format(
                    run_id,
                    run["status"],
                    run["items_seen"],
                    run["unique_items"],
                    run["duplicate_items"],
                    run["error_message"] or "-",
                )
            )
            return
        time.sleep(0.5)


if __name__ == "__main__":
    main()
