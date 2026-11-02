"""Offline acceptance check: replay a read-only case database into a temporary bank.

Never accesses the website, edits the input bank or copies private payloads to the report.
"""
import argparse
import collections
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from medik_pilot.domain import CollectedItem, case_content_identity
from medik_pilot.runner import RunManager
from medik_pilot.storage import Storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--immutable-snapshot", action="store_true", help="Only for a static DB extracted from an archive without WAL files")
    args = parser.parse_args()
    # Ordinary readonly mode includes any source WAL. Never assume immutable
    # for a potentially active database supplied by an operator.
    options = "?mode=ro"
    if args.immutable_snapshot:
        assert not Path(str(args.database) + "-wal").exists(), "WAL exists: do not open as immutable"
        options += "&immutable=1"
    source = sqlite3.connect(args.database.resolve().as_uri() + options, uri=True)
    try:
        assert source.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = source.execute("SELECT source_mode,specialty,source_id,payload_json FROM items WHERE kind='case' AND status='ready' ORDER BY id").fetchall()
    finally:
        source.close()
    assert rows, "No ready cases in input"
    grouped = collections.defaultdict(list)
    for mode, specialty, source_id, raw in rows:
        grouped[(mode, specialty)].append(CollectedItem("case", source_id, json.loads(raw)))
    reports = []
    for (mode, specialty), items in grouped.items():
        identities = {case_content_identity(item.payload) for item in items}
        # The audit must not conflate changed versions of one source ID.
        assert len({item.source_id for item in items}) == len(items), "Versioned input needs a separate version-aware audit"
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="medik-case-replay-") as directory:
            storage = Storage(Path(directory) / "qa.db")
            config = dict(source_mode=mode, material_type="case", specialty=specialty,
                          reference_cases=len(items) + 1, reference_tests=0,
                          verification_percent=0, max_attempts=len(items),
                          max_requests=len(items) + 1, max_duration_minutes=120,
                          document_mode="new", delay_seconds=0)
            class ReplayCollector:
                def collect_attempt(self, kind, attempt):
                    if attempt % 100 == 0:
                        print("Processed input {} / {}".format(attempt, len(items)), flush=True)
                    return [items[attempt - 1]]

            class OfflineManager(RunManager):
                def _collector(self, mode, config, run_id=None):
                    return ReplayCollector()

            storage.create_run("offline-replay", config)
            OfflineManager(storage)._execute("offline-replay", config)
            run = storage.get_run("offline-replay")
            exported = storage.export_rows("offline-replay")
            bank = storage.current_bank_rows(specialty)
            expected = len(identities)
            assert run["stop_reason"] == "max_attempts", run["stop_reason"]
            assert run["attempts_completed"] == len(items)
            assert run["collected_by_kind"].get("case") == expected
            assert run["checkpoint"]["run_counts"]["case"] == expected
            assert len(exported) == len(bank) == expected
            assert {case_content_identity(row["payload"]) for row in exported} == identities
            assert run["unique_items"] == expected
            assert run["duplicate_items"] == len(items) - expected
            with storage.database() as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                assert integrity == "ok"
            reports.append(dict(specialty=specialty, received=len(items), unique_ready=expected,
                                duplicates=run["duplicate_items"], run_export_rows=len(exported),
                                bank_export_rows=len(bank), integrity=integrity,
                                attempts=run["attempts_completed"], stop_reason=run["stop_reason"],
                                elapsed_seconds=round(time.monotonic() - started, 2)))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"mode":"offline_saved_case_replay", "passed":True, "results":reports}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(reports, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
