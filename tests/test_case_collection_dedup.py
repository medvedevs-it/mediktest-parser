import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medik_pilot.collectors.demo import DemoCollector
from medik_pilot.domain import CollectedItem
from medik_pilot.runner import RunManager
from medik_pilot.storage import Storage


class CaseCollectionDedupTests(unittest.TestCase):
    def test_new_run_reuses_an_existing_bank_case_without_counting_attempt_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "qa.db")
            body = DemoCollector(specialty="Лечебное дело").collect_attempt("case", 1)[0].payload
            config = dict(source_mode="live", material_type="case", specialty="Лечебное дело",
                          reference_cases=2, reference_tests=0, verification_percent=0,
                          max_attempts=3, max_requests=100, max_duration_minutes=5,
                          document_mode="new", delay_seconds=0)
            storage.create_run("old", config)
            _, old_id = storage.store_item("old", "live", config["specialty"],
                                            CollectedItem("case", "original", body), 1)
            class Collector:
                def collect_attempt(self, kind, attempt):
                    changed_order = copy.deepcopy(body)
                    changed_order["questions"][0]["options"].reverse()
                    return [CollectedItem("case", "new-attempt-{}".format(attempt), changed_order)]

            storage.create_run("new", config)
            manager = RunManager(storage)
            with patch.object(manager, "_collector", return_value=Collector()):
                manager._execute("new", config)
            run = storage.get_run("new")
            self.assertEqual(run["stop_reason"], "max_attempts")
            self.assertEqual(run["unique_items"], 0)
            self.assertEqual(run["duplicate_items"], 3)
            self.assertEqual(run["collected_by_kind"], {"case": 1})
            self.assertEqual(run["checkpoint"]["run_counts"]["case"], 1)
            self.assertEqual(storage.catalog_counts("live", config["specialty"]), {"case": 1})
            with storage.database() as db:
                saved_ids = {row[0] for row in db.execute("SELECT item_id FROM run_items WHERE run_id='new'")}
            self.assertEqual(saved_ids, {old_id})

    def test_case_repeats_do_not_fill_goal_or_hide_later_new_case(self):
        for specialty in ("Лечебное дело", "Педиатрия"):
            with self.subTest(specialty=specialty), tempfile.TemporaryDirectory() as directory:
                storage = Storage(Path(directory) / "qa.db")
                original = DemoCollector(specialty=specialty).collect_attempt("case", 1)[0].payload
                config = dict(source_mode="live", material_type="case", specialty=specialty,
                              reference_cases=2, reference_tests=0, verification_percent=0,
                              max_attempts=4, max_requests=100, max_duration_minutes=5,
                              document_mode="new", delay_seconds=0)

                class Collector:
                    def collect_attempt(self, kind, attempt):
                        body = copy.deepcopy(original)
                        if attempt == 4:
                            body["questions"][0]["question"] += " Другой вопрос"
                        elif attempt > 1:
                            body["questions"][0]["options"].reverse()
                        return [CollectedItem("case", "attempt-{}".format(attempt), body)]

                storage.create_run("run", config)
                manager = RunManager(storage)
                with patch.object(manager, "_collector", return_value=Collector()):
                    manager._execute("run", config)
                run = storage.get_run("run")
                self.assertEqual(run["status"], "completed", run.get("error_message"))
                self.assertEqual(run["attempts_completed"], 4)
                self.assertEqual(run["duplicate_items"], 2)
                self.assertEqual(run["unique_items"], 2)
                self.assertEqual(run["collected_by_kind"], {"case": 2})
                self.assertEqual(run["checkpoint"]["run_counts"]["case"], 2)
                self.assertEqual(len(storage.export_rows("run")), 2)

    def test_restart_uses_the_same_saved_case_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "qa.db")
            body = DemoCollector(specialty="Лечебное дело").collect_attempt("case", 1)[0].payload
            config = dict(source_mode="live", material_type="case", specialty="Лечебное дело",
                          reference_cases=2, reference_tests=0, verification_percent=0,
                          max_attempts=1, max_requests=100, max_duration_minutes=5,
                          document_mode="new", delay_seconds=0)

            class Collector:
                def collect_attempt(self, kind, attempt):
                    return [CollectedItem("case", "attempt-{}".format(attempt), copy.deepcopy(body))]

            storage.create_run("run", config)
            first = RunManager(storage)
            with patch.object(first, "_collector", return_value=Collector()):
                first._execute("run", config)
            second = RunManager(Storage(storage.path))
            config["max_attempts"] = 3
            with patch.object(second, "_collector", return_value=Collector()):
                second._execute("run", config)
            run = storage.get_run("run")
            self.assertEqual(run["stop_reason"], "max_attempts")
            self.assertEqual(run["status"], "partial")
            self.assertEqual(run["checkpoint"]["run_counts"]["case"], 1)
            self.assertEqual(run["collected_by_kind"], {"case": 1})
            self.assertEqual(len(storage.export_rows("run")), 1)
            self.assertEqual(run["duplicate_items"], 2)
