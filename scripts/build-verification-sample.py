"""Build a deterministic full-version demo export for automated and visual QA."""

import json
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "outputs" / "full-version-verification" / "runtime"
os.environ["MEDIKTEST_DATA_DIR"] = str(OUTPUT_ROOT)
sys.path.insert(0, str(ROOT))

from medik_pilot.exporter import export_json, export_xlsx
from medik_pilot.runner import RunManager
from medik_pilot.storage import Storage


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    storage = Storage(OUTPUT_ROOT / "verification.db")
    manager = RunManager(storage)
    run_id = manager.start(
        {
            "source_mode": "demo",
            "material_type": "both",
            "specialty": "Лечебное дело",
            "reference_tests": 10,
            "reference_cases": 3,
            "verification_percent": 15,
            "max_attempts": 10,
            "max_requests": 100,
            "max_duration_minutes": 10,
            "allow_create_attempts": False,
            "allow_answer_submission": False,
            "delay_seconds": 0,
        }
    )
    for _ in range(500):
        run = storage.get_run(run_id)
        if run and run["status"] in {"completed", "partial", "failed", "stopped"}:
            break
        time.sleep(0.01)
    else:
        raise RuntimeError("Демонстрационный запуск не завершился за контрольное время.")
    if run["status"] != "completed":
        raise RuntimeError("Демонстрационный запуск завершился со статусом {}.".format(run["status"]))
    result = {
        "run_id": run_id,
        "json": str(export_json(storage, run_id)),
        "xlsx": str(export_xlsx(storage, run_id)),
    }
    (OUTPUT_ROOT / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
