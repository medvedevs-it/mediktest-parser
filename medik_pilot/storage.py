import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import DB_PATH, ensure_directories
from .domain import CollectedItem, payload_status
from .images import iter_image_assets
from .specialties import normalize_specialty


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: Path = DB_PATH):
        ensure_directories()
        self.path = Path(path)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back the transaction and always release the DB file."""
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.database() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    source_mode TEXT NOT NULL,
                    material_type TEXT NOT NULL,
                    specialty TEXT NOT NULL,
                    document_mode TEXT NOT NULL DEFAULT 'catalog',
                    document_name TEXT NOT NULL DEFAULT 'МедикТест Лечебное дело',
                    status TEXT NOT NULL,
                    stop_reason TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    max_attempts INTEGER NOT NULL,
                    max_tests INTEGER NOT NULL DEFAULT -1,
                    max_cases INTEGER NOT NULL DEFAULT -1,
                    reference_tests INTEGER NOT NULL DEFAULT 0,
                    reference_cases INTEGER NOT NULL DEFAULT 0,
                    verification_percent INTEGER NOT NULL DEFAULT 15,
                    max_requests INTEGER NOT NULL DEFAULT 1000,
                    allow_create_attempts INTEGER NOT NULL DEFAULT 0,
                    allow_answer_submission INTEGER NOT NULL DEFAULT 0,
                    max_duration_minutes INTEGER NOT NULL,
                    saturation_window INTEGER NOT NULL,
                    novelty_threshold REAL NOT NULL,
                    delay_seconds REAL NOT NULL,
                    attempts_completed INTEGER NOT NULL DEFAULT 0,
                    items_seen INTEGER NOT NULL DEFAULT 0,
                    unique_items INTEGER NOT NULL DEFAULT 0,
                    duplicate_items INTEGER NOT NULL DEFAULT 0,
                    changed_items INTEGER NOT NULL DEFAULT 0,
                    requests_made INTEGER NOT NULL DEFAULT 0,
                    elapsed_seconds REAL NOT NULL DEFAULT 0,
                    checkpoint_json TEXT,
                    error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_mode TEXT NOT NULL,
                    specialty TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    raw_payload_json TEXT,
                    status TEXT NOT NULL DEFAULT 'incomplete',
                    first_seen_at TEXT NOT NULL,
                    version_created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(source_mode, specialty, kind, source_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_items_identity
                    ON items(source_mode, specialty, kind, source_id);
                CREATE INDEX IF NOT EXISTS idx_items_hash
                    ON items(source_mode, specialty, kind, content_hash);
                CREATE TABLE IF NOT EXISTS run_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    attempt_number INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempt_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    attempt_number INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    seen INTEGER NOT NULL,
                    new_items INTEGER NOT NULL,
                    duplicates INTEGER NOT NULL,
                    changed INTEGER NOT NULL,
                    novelty_rate REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_entity_ids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_mode TEXT NOT NULL,
                    specialty TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    client_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_mode, specialty, entity_type, stable_key),
                    UNIQUE(source_mode, specialty, entity_type, client_id)
                );
                CREATE TABLE IF NOT EXISTS raw_captures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    raw_payload_json TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    UNIQUE(run_id, kind, source_id, attempt_number)
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
            if "max_tests" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN max_tests INTEGER NOT NULL DEFAULT 10")
            if "document_mode" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN document_mode TEXT NOT NULL DEFAULT 'catalog'")
            if "document_name" not in columns:
                db.execute(
                    "ALTER TABLE runs ADD COLUMN document_name TEXT NOT NULL "
                    "DEFAULT 'МедикТест Лечебное дело'"
                )
            if "max_cases" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN max_cases INTEGER NOT NULL DEFAULT -1")
            if "max_requests" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN max_requests INTEGER NOT NULL DEFAULT 1000")
            if "reference_tests" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN reference_tests INTEGER NOT NULL DEFAULT 0")
            if "reference_cases" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN reference_cases INTEGER NOT NULL DEFAULT 0")
            if "verification_percent" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN verification_percent INTEGER NOT NULL DEFAULT 15")
            if "allow_create_attempts" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN allow_create_attempts INTEGER NOT NULL DEFAULT 0")
            if "allow_answer_submission" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN allow_answer_submission INTEGER NOT NULL DEFAULT 0")
            if "requests_made" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN requests_made INTEGER NOT NULL DEFAULT 0")
            if "elapsed_seconds" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN elapsed_seconds REAL NOT NULL DEFAULT 0")
            if "checkpoint_json" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN checkpoint_json TEXT")
            db.execute(
                """UPDATE runs SET reference_tests = max_tests
                   WHERE reference_tests = 0 AND max_tests > 0"""
            )
            db.execute(
                """UPDATE runs SET reference_cases = max_cases
                   WHERE reference_cases = 0 AND max_cases > 0"""
            )
            item_columns = {row["name"] for row in db.execute("PRAGMA table_info(items)").fetchall()}
            if "raw_payload_json" not in item_columns:
                db.execute("ALTER TABLE items ADD COLUMN raw_payload_json TEXT")
            if "status" not in item_columns:
                db.execute("ALTER TABLE items ADD COLUMN status TEXT NOT NULL DEFAULT 'incomplete'")
            if "version_created_at" not in item_columns:
                db.execute("ALTER TABLE items ADD COLUMN version_created_at TEXT")
                db.execute(
                    "UPDATE items SET version_created_at = first_seen_at "
                    "WHERE version_created_at IS NULL"
                )
            legacy_items = db.execute(
                "SELECT id, kind, payload_json FROM items WHERE status = 'incomplete' OR raw_payload_json IS NULL"
            ).fetchall()
            for row in legacy_items:
                payload = json.loads(row["payload_json"])
                db.execute(
                    """UPDATE items
                       SET status = ?, raw_payload_json = COALESCE(raw_payload_json, payload_json)
                       WHERE id = ?""",
                    (payload_status(row["kind"], payload), row["id"]),
                )

    def recover_stale_runs(self) -> None:
        """Keep interrupted runs resumable after the application restarts."""
        with self._lock, self.database() as db:
            db.execute(
                """UPDATE runs
                   SET status = 'paused', stop_reason = 'application_restart',
                       error_message = COALESCE(error_message, 'Сбор приостановлен после перезапуска приложения.')
                   WHERE status IN ('queued', 'running', 'stopping')"""
            )

    def reset_all_materials(self) -> Dict[str, int]:
        """Delete the complete material bank and its run history atomically."""
        with self._lock, self.database() as db:
            counts = {
                row["kind"]: int(row["count"] or 0)
                for row in db.execute(
                    "SELECT kind, COUNT(*) AS count FROM ("
                    "SELECT DISTINCT source_mode, specialty, kind, source_id FROM items"
                    ") identities GROUP BY kind"
                ).fetchall()
            }
            run_count = int(
                db.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"] or 0
            )
            db.execute("DELETE FROM raw_captures")
            db.execute("DELETE FROM events")
            db.execute("DELETE FROM attempt_stats")
            db.execute("DELETE FROM run_items")
            db.execute("DELETE FROM client_entity_ids")
            db.execute("DELETE FROM items")
            db.execute("DELETE FROM runs")
            db.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('raw_captures', 'events', 'attempt_stats', 'run_items', "
                "'client_entity_ids', 'items')"
            )
        return {
            "deleted_tests": counts.get("test", 0),
            "deleted_cases": counts.get("case", 0),
            "deleted_runs": run_count,
        }

    def reset_specialty_materials(self, specialty: str) -> Dict[str, Any]:
        """Remove one specialty bank while preserving every other specialty."""
        specialty = normalize_specialty(specialty)
        with self._lock, self.database() as db:
            run_rows = db.execute(
                "SELECT id FROM runs WHERE specialty = ?", (specialty,)
            ).fetchall()
            run_ids = [str(row["id"]) for row in run_rows]
            item_rows = db.execute(
                """SELECT id, kind, source_id, payload_json FROM items
                   WHERE specialty = ?""",
                (specialty,),
            ).fetchall()
            counts = {
                "test": len({row["source_id"] for row in item_rows if row["kind"] == "test"}),
                "case": len({row["source_id"] for row in item_rows if row["kind"] == "case"}),
            }
            item_ids = [int(row["id"]) for row in item_rows]
            if run_ids:
                marks = ",".join("?" for _ in run_ids)
                for table in ("raw_captures", "events", "attempt_stats", "run_items"):
                    db.execute(
                        "DELETE FROM {} WHERE run_id IN ({})".format(table, marks),
                        run_ids,
                    )
            if item_ids:
                marks = ",".join("?" for _ in item_ids)
                db.execute("DELETE FROM run_items WHERE item_id IN ({})".format(marks), item_ids)
            db.execute("DELETE FROM runs WHERE specialty = ?", (specialty,))
            db.execute("DELETE FROM items WHERE specialty = ?", (specialty,))
            db.execute("DELETE FROM client_entity_ids WHERE specialty = ?", (specialty,))
            return {
                "specialty": specialty,
                "deleted_tests": counts["test"],
                "deleted_cases": counts["case"],
                "deleted_runs": len(run_ids),
                "deleted_run_ids": run_ids,
            }

    def referenced_image_storage_names(self) -> set[str]:
        """Return content-addressed image files still referenced by any item."""
        names: set[str] = set()
        with self.database() as db:
            rows = db.execute("SELECT payload_json AS value FROM items").fetchall()
            rows += db.execute(
                "SELECT payload_json AS value FROM raw_captures UNION ALL "
                "SELECT raw_payload_json AS value FROM raw_captures"
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
            for asset in iter_image_assets(payload):
                storage_name = str(asset.get("storage_name") or "").strip()
                if storage_name:
                    names.add(storage_name)
        return names

    def create_run(self, run_id: str, config: Dict[str, Any]) -> None:
        with self._lock, self.database() as db:
            db.execute(
                """INSERT INTO runs (
                    id, source_mode, material_type, specialty, document_mode, document_name,
                    status, created_at,
                    max_attempts, max_tests, max_cases, reference_tests, reference_cases,
                    verification_percent, max_requests, allow_create_attempts, allow_answer_submission,
                    max_duration_minutes, saturation_window, novelty_threshold, delay_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    config["source_mode"],
                    config["material_type"],
                    config["specialty"],
                    config.get("document_mode", "catalog"),
                    config.get("document_name", "МедикТест Лечебное дело"),
                    utc_now(),
                    config["max_attempts"],
                    config.get("reference_tests", config.get("max_tests", 0)),
                    config.get("reference_cases", config.get("max_cases", 0)),
                    config.get("reference_tests", config.get("max_tests", 0)),
                    config.get("reference_cases", config.get("max_cases", 0)),
                    config.get("verification_percent", 15),
                    config.get("max_requests", 1000),
                    1 if config.get("allow_create_attempts", False) else 0,
                    1 if config.get("allow_answer_submission", False) else 0,
                    config["max_duration_minutes"],
                    config.get("saturation_window", 3),
                    config.get("novelty_threshold", 0.0),
                    config["delay_seconds"],
                ),
            )

    def update_run(self, run_id: str, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "status", "stop_reason", "started_at", "finished_at", "attempts_completed",
            "items_seen", "unique_items", "duplicate_items", "changed_items", "error_message",
            "requests_made", "elapsed_seconds", "checkpoint_json",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("Unsupported run fields: " + ", ".join(sorted(unknown)))
        columns = ", ".join("{} = ?".format(name) for name in values)
        with self._lock, self.database() as db:
            db.execute("UPDATE runs SET {} WHERE id = ?".format(columns), (*values.values(), run_id))

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.database() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["allow_create_attempts"] = bool(result.get("allow_create_attempts"))
            result["allow_answer_submission"] = bool(result.get("allow_answer_submission"))
            result["checkpoint"] = json.loads(result["checkpoint_json"]) if result.get("checkpoint_json") else {}
            result["events"] = [
                dict(item) for item in db.execute(
                    "SELECT level, message, created_at FROM events WHERE run_id = ? ORDER BY id DESC LIMIT 30",
                    (run_id,),
                ).fetchall()
            ]
            result["attempt_stats"] = [
                dict(item) for item in db.execute(
                    "SELECT attempt_number, kind, seen, new_items, duplicates, changed, novelty_rate, created_at "
                    "FROM attempt_stats WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
            ]
            result["error_count"] = int(
                db.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE run_id = ? AND level = 'error'",
                    (run_id,),
                ).fetchone()["count"]
            )
            result["raw_capture_count"] = int(
                db.execute(
                    "SELECT COUNT(*) AS count FROM raw_captures WHERE run_id = ?",
                    (run_id,),
                ).fetchone()["count"]
            )
            result["collected_by_kind"] = {
                item["kind"]: item["count"]
                for item in db.execute(
                    """SELECT i.kind, COUNT(DISTINCT i.source_id) AS count
                       FROM run_items ri JOIN items i ON i.id = ri.item_id
                       WHERE ri.run_id = ? AND i.status = 'ready' GROUP BY i.kind""",
                    (run_id,),
                ).fetchall()
            }
            result["catalog_counts"] = {
                item["kind"]: item["count"]
                for item in db.execute(
                    """SELECT current.kind, COUNT(*) AS count FROM items current
                       WHERE current.source_mode = ? AND current.specialty = ?
                         AND current.status = 'ready'
                         AND current.version = (
                           SELECT MAX(latest.version) FROM items latest
                           WHERE latest.source_mode = current.source_mode
                             AND latest.specialty = current.specialty
                             AND latest.kind = current.kind
                             AND latest.source_id = current.source_id
                         )
                       GROUP BY current.kind""",
                    (result["source_mode"], result["specialty"]),
                ).fetchall()
            }
            audit_rows = db.execute(
                """WITH current AS (
                       SELECT latest_item.kind, latest_item.source_id
                       FROM items latest_item
                       WHERE latest_item.source_mode = ? AND latest_item.specialty = ?
                         AND latest_item.status = 'ready'
                         AND latest_item.version = (
                           SELECT MAX(candidate.version) FROM items candidate
                           WHERE candidate.source_mode = latest_item.source_mode
                             AND candidate.specialty = latest_item.specialty
                             AND candidate.kind = latest_item.kind
                             AND candidate.source_id = latest_item.source_id
                         )
                   ), seen AS (
                       SELECT DISTINCT seen_item.kind, seen_item.source_id
                       FROM run_items ri JOIN items seen_item ON seen_item.id = ri.item_id
                       WHERE ri.run_id = ? AND seen_item.status = 'ready'
                         AND ri.outcome IN ('new', 'duplicate', 'changed')
                   )
                   SELECT current.kind, COUNT(*) AS total,
                          SUM(CASE WHEN seen.source_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed
                   FROM current
                   LEFT JOIN seen ON seen.kind = current.kind AND seen.source_id = current.source_id
                   GROUP BY current.kind""",
                (result["source_mode"], result["specialty"], run_id),
            ).fetchall()
            result["catalog_audit"] = {
                row["kind"]: {
                    "total": int(row["total"] or 0),
                    "confirmed": int(row["confirmed"] or 0),
                    "not_checked": int(row["total"] or 0) - int(row["confirmed"] or 0),
                }
                for row in audit_rows
            }
            return result

    def run_config(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = self.get_run(run_id)
        if not run:
            return None
        return {
            "source_mode": run["source_mode"],
            "material_type": run["material_type"],
            "specialty": run["specialty"],
            "document_mode": run.get("document_mode", "catalog"),
            "document_name": run.get("document_name", "МедикТест Лечебное дело"),
            "reference_tests": run.get("reference_tests", run.get("max_tests", 0)),
            "reference_cases": run.get("reference_cases", run.get("max_cases", 0)),
            "verification_percent": run.get("verification_percent", 15),
            "max_attempts": run["max_attempts"],
            "max_requests": run.get("max_requests", 1000),
            "max_duration_minutes": run["max_duration_minutes"],
            "allow_create_attempts": bool(run.get("allow_create_attempts")),
            "allow_answer_submission": bool(run.get("allow_answer_submission")),
            "delay_seconds": run["delay_seconds"],
            "elapsed_seconds": float(run.get("elapsed_seconds") or 0),
        }

    def latest_run(self) -> Optional[Dict[str, Any]]:
        with self.database() as db:
            row = db.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        return self.get_run(row["id"]) if row else None

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.database() as db:
            rows = db.execute(
                """SELECT id, source_mode, material_type, specialty, document_mode, document_name,
                          status, stop_reason,
                          created_at, started_at, finished_at, reference_tests, reference_cases,
                          items_seen, unique_items, duplicate_items, changed_items, requests_made,
                          elapsed_seconds,
                          (SELECT COUNT(*) FROM events WHERE events.run_id = runs.id
                           AND events.level = 'error') AS error_count
                   FROM runs ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_ready_source_ids(self, run_id: str) -> Dict[str, set[str]]:
        with self.database() as db:
            rows = db.execute(
                """SELECT i.kind, i.source_id
                   FROM run_items ri JOIN items i ON i.id = ri.item_id
                   WHERE ri.run_id = ? AND i.status = 'ready'
                   GROUP BY i.kind, i.source_id""",
                (run_id,),
            ).fetchall()
        result: Dict[str, set[str]] = {"test": set(), "case": set()}
        for row in rows:
            result.setdefault(row["kind"], set()).add(row["source_id"])
        return result

    def seed_run_from_catalog(
        self,
        run_id: str,
        source_mode: str,
        specialty: str,
        limits: Dict[str, int],
    ) -> Dict[str, int]:
        """Attach a random ready catalog selection to a new-document run.

        Selecting an existing item does not update ``last_seen_at`` because it
        is not proof that the external source still exposes that item.
        """
        selected = {"test": 0, "case": 0}
        now = utc_now()
        with self._lock, self.database() as db:
            for kind in ("test", "case"):
                limit = max(0, int(limits.get(kind, 0)))
                if not limit:
                    continue
                rows = db.execute(
                    """SELECT current.id
                       FROM items current
                       WHERE current.source_mode = ? AND current.specialty = ?
                         AND current.kind = ? AND current.status = 'ready'
                         AND current.version = (
                           SELECT MAX(latest.version) FROM items latest
                           WHERE latest.source_mode = current.source_mode
                             AND latest.specialty = current.specialty
                             AND latest.kind = current.kind
                             AND latest.source_id = current.source_id
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM run_items existing_link
                           JOIN items existing_item ON existing_item.id = existing_link.item_id
                           WHERE existing_link.run_id = ?
                             AND existing_item.kind = current.kind
                             AND existing_item.source_id = current.source_id
                         )
                       ORDER BY RANDOM()
                       LIMIT ?""",
                    (source_mode, specialty, kind, run_id, limit),
                ).fetchall()
                for row in rows:
                    db.execute(
                        """INSERT INTO run_items (
                               run_id, item_id, attempt_number, outcome, seen_at
                           ) VALUES (?, ?, 0, 'selected', ?)""",
                        (run_id, row["id"], now),
                    )
                selected[kind] = len(rows)
        return selected

    def catalog_counts(self, source_mode: str, specialty: str) -> Dict[str, int]:
        with self.database() as db:
            rows = db.execute(
                """SELECT kind, COUNT(*) AS count FROM items current
                   WHERE source_mode = ? AND specialty = ?
                     AND status = 'ready'
                     AND version = (
                       SELECT MAX(version) FROM items latest
                       WHERE latest.source_mode = current.source_mode
                         AND latest.specialty = current.specialty
                         AND latest.kind = current.kind
                         AND latest.source_id = current.source_id
                     )
                   GROUP BY kind""",
                (source_mode, specialty),
            ).fetchall()
        return {row["kind"]: row["count"] for row in rows}

    def catalog_audit_items(
        self,
        run_id: str,
        kind: str = "all",
        audit_status: str = "not_checked",
        limit: int = 200,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return the current ready catalog with confirmation state for one run.

        A random run can confirm that an item is still present, but absence from
        that run is only reported as ``not_checked``. It is never interpreted as
        deletion.
        """
        run = self.get_run(run_id)
        if not run:
            raise ValueError("Run not found")
        if kind not in {"all", "test", "case"}:
            raise ValueError("Unsupported material kind")
        if audit_status not in {"all", "confirmed", "not_checked"}:
            raise ValueError("Unsupported audit status")
        limit = max(1, min(int(limit), 100000))
        offset = max(0, int(offset))
        with self.database() as db:
            rows = db.execute(
                """WITH current AS (
                       SELECT latest_item.*
                       FROM items latest_item
                       WHERE latest_item.source_mode = ? AND latest_item.specialty = ?
                         AND latest_item.status = 'ready'
                         AND latest_item.version = (
                           SELECT MAX(candidate.version) FROM items candidate
                           WHERE candidate.source_mode = latest_item.source_mode
                             AND candidate.specialty = latest_item.specialty
                             AND candidate.kind = latest_item.kind
                             AND candidate.source_id = latest_item.source_id
                         )
                   ), run_seen AS (
                       SELECT seen_item.kind, seen_item.source_id, COUNT(*) AS run_occurrences
                       FROM run_items ri JOIN items seen_item ON seen_item.id = ri.item_id
                       WHERE ri.run_id = ? AND seen_item.status = 'ready'
                         AND ri.outcome IN ('new', 'duplicate', 'changed')
                       GROUP BY seen_item.kind, seen_item.source_id
                   ), lifetime AS (
                       SELECT seen_item.source_mode, seen_item.specialty,
                              seen_item.kind, seen_item.source_id,
                              COUNT(*) AS times_seen
                       FROM run_items ri JOIN items seen_item ON seen_item.id = ri.item_id
                       WHERE ri.outcome IN ('new', 'duplicate', 'changed')
                       GROUP BY seen_item.source_mode, seen_item.specialty,
                                seen_item.kind, seen_item.source_id
                   )
                   SELECT current.kind, current.source_id, current.version,
                          current.payload_json, current.first_seen_at, current.last_seen_at,
                          COALESCE(lifetime.times_seen, 0) AS times_seen,
                          CASE WHEN run_seen.source_id IS NULL THEN 0 ELSE 1 END AS confirmed
                   FROM current
                   LEFT JOIN run_seen
                     ON run_seen.kind = current.kind
                    AND run_seen.source_id = current.source_id
                   LEFT JOIN lifetime
                     ON lifetime.source_mode = current.source_mode
                    AND lifetime.specialty = current.specialty
                    AND lifetime.kind = current.kind
                    AND lifetime.source_id = current.source_id
                   WHERE (? = 'all' OR current.kind = ?)
                     AND (
                       ? = 'all'
                       OR (? = 'confirmed' AND run_seen.source_id IS NOT NULL)
                       OR (? = 'not_checked' AND run_seen.source_id IS NULL)
                     )
                   ORDER BY confirmed ASC, current.last_seen_at ASC,
                            current.kind, current.source_id""",
                (
                    run["source_mode"], run["specialty"], run_id,
                    kind, kind,
                    audit_status, audit_status, audit_status,
                ),
            ).fetchall()
        total = len(rows)
        page = rows[offset:offset + limit]
        items = []
        for row in page:
            payload = json.loads(row["payload_json"])
            title = (
                payload.get("question")
                if row["kind"] == "test"
                else payload.get("header") or payload.get("condition")
            )
            items.append({
                "kind": row["kind"],
                "source_id": row["source_id"],
                "version": int(row["version"]),
                "title": str(title or "").strip(),
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "times_seen": int(row["times_seen"] or 0),
                "audit_status": "confirmed" if row["confirmed"] else "not_checked",
            })
        return {
            "run_id": run_id,
            "kind": kind,
            "audit_status": audit_status,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
            "deletion_confirmed": False,
        }

    def add_event(self, run_id: str, message: str, level: str = "info") -> None:
        with self._lock, self.database() as db:
            db.execute(
                "INSERT INTO events (run_id, level, message, created_at) VALUES (?, ?, ?, ?)",
                (run_id, level, message, utc_now()),
            )

    def store_item(
        self,
        run_id: str,
        source_mode: str,
        specialty: str,
        item: CollectedItem,
        attempt_number: int,
    ) -> Tuple[str, int]:
        now = utc_now()
        status = payload_status(item.kind, item.payload)
        payload_json = json.dumps(item.payload, ensure_ascii=False)
        raw_payload_json = json.dumps(item.raw_payload or item.payload, ensure_ascii=False)
        with self._lock, self.database() as db:
            previous = db.execute(
                """SELECT * FROM items
                   WHERE source_mode = ? AND specialty = ? AND kind = ? AND source_id = ?
                   ORDER BY version DESC LIMIT 1""",
                (source_mode, specialty, item.kind, item.source_id),
            ).fetchone()
            if previous and previous["content_hash"] == item.content_hash:
                outcome, item_id = "duplicate", previous["id"]
                db.execute("UPDATE items SET last_seen_at = ? WHERE id = ?", (now, item_id))
            elif not previous:
                same_content = db.execute(
                    """SELECT * FROM items
                       WHERE source_mode = ? AND specialty = ? AND kind = ? AND content_hash = ?
                       ORDER BY last_seen_at DESC LIMIT 1""",
                    (source_mode, specialty, item.kind, item.content_hash),
                ).fetchone()
                if same_content:
                    outcome, item_id = "duplicate", same_content["id"]
                    db.execute("UPDATE items SET last_seen_at = ? WHERE id = ?", (now, item_id))
                else:
                    cursor = db.execute(
                    """INSERT INTO items (
                            source_mode, specialty, kind, source_id, content_hash, version,
                            payload_json, raw_payload_json, status, first_seen_at,
                            version_created_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            source_mode, specialty, item.kind, item.source_id, item.content_hash,
                            1, payload_json, raw_payload_json, status, now, now, now,
                        ),
                    )
                    outcome, item_id = "new", cursor.lastrowid
            else:
                version = previous["version"] + 1
                cursor = db.execute(
                    """INSERT INTO items (
                        source_mode, specialty, kind, source_id, content_hash, version,
                        payload_json, raw_payload_json, status, first_seen_at,
                        version_created_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source_mode, specialty, item.kind, item.source_id, item.content_hash,
                        version, payload_json, raw_payload_json, status,
                        previous["first_seen_at"], now, now,
                    ),
                )
                outcome, item_id = "changed", cursor.lastrowid
            db.execute(
                "INSERT INTO run_items (run_id, item_id, attempt_number, outcome, seen_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, item_id, attempt_number, outcome, now),
            )
        return outcome, int(item_id)

    def stage_raw_item(self, run_id: str, item: CollectedItem, attempt_number: int) -> None:
        """Persist a recoverable raw capture before the collector finishes an attempt."""
        now = utc_now()
        with self._lock, self.database() as db:
            db.execute(
                """INSERT INTO raw_captures (
                       run_id, kind, source_id, attempt_number,
                       payload_json, raw_payload_json, captured_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, kind, source_id, attempt_number) DO UPDATE SET
                       payload_json = excluded.payload_json,
                       raw_payload_json = excluded.raw_payload_json,
                       captured_at = excluded.captured_at""",
                (
                    run_id,
                    item.kind,
                    item.source_id,
                    attempt_number,
                    json.dumps(item.payload, ensure_ascii=False),
                    json.dumps(item.raw_payload or item.payload, ensure_ascii=False),
                    now,
                ),
            )

    def add_attempt_stats(self, run_id: str, attempt: int, kind: str, counts: Dict[str, int]) -> None:
        seen = counts["new"] + counts["duplicate"] + counts["changed"]
        rate = counts["new"] / seen if seen else 0.0
        with self._lock, self.database() as db:
            db.execute(
                """INSERT INTO attempt_stats (
                    run_id, attempt_number, kind, seen, new_items, duplicates,
                    changed, novelty_rate, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, attempt, kind, seen, counts["new"], counts["duplicate"], counts["changed"], rate, utc_now()),
            )

    def export_rows(self, run_id: str) -> List[Dict[str, Any]]:
        with self.database() as db:
            rows = db.execute(
                """SELECT i.*, ri.attempt_number, ri.outcome, ri.seen_at
                   FROM run_items ri JOIN items i ON i.id = ri.item_id
                   WHERE ri.run_id = ? ORDER BY i.kind, i.source_id, i.version""",
                (run_id,),
            ).fetchall()
        unique: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in rows:
            key = (row["kind"], row["source_id"])
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            raw_payload = item.pop("raw_payload_json", None)
            item["raw_payload"] = json.loads(raw_payload) if raw_payload else item["payload"]
            if key not in unique or item["version"] > unique[key]["version"]:
                unique[key] = item
        return list(unique.values())

    def catalog_rows_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        """Return the complete catalog snapshot as it existed when a run ended."""
        with self.database() as db:
            run = db.execute(
                "SELECT source_mode, specialty, finished_at, created_at FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                return []
            cutoff = run["finished_at"] or utc_now()
            rows = db.execute(
                """SELECT current.*
                   FROM items current
                   WHERE current.source_mode = ?
                     AND current.specialty = ?
                     AND current.version_created_at <= ?
                     AND current.version = (
                       SELECT MAX(latest.version)
                       FROM items latest
                       WHERE latest.source_mode = current.source_mode
                         AND latest.specialty = current.specialty
                         AND latest.kind = current.kind
                         AND latest.source_id = current.source_id
                         AND latest.version_created_at <= ?
                     )
                   ORDER BY current.kind, current.source_id""",
                (run["source_mode"], run["specialty"], cutoff, cutoff),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            raw_payload = item.pop("raw_payload_json", None)
            item["raw_payload"] = json.loads(raw_payload) if raw_payload else item["payload"]
            result.append(item)
        return result

    def client_entity_id(
        self,
        source_mode: str,
        specialty: str,
        entity_type: str,
        stable_key: str,
        start_at: int = 1,
    ) -> int:
        with self._lock, self.database() as db:
            existing = db.execute(
                """SELECT client_id FROM client_entity_ids
                   WHERE source_mode = ? AND specialty = ? AND entity_type = ? AND stable_key = ?""",
                (source_mode, specialty, entity_type, stable_key),
            ).fetchone()
            if existing:
                return int(existing["client_id"])
            row = db.execute(
                """SELECT MAX(client_id) AS maximum FROM client_entity_ids
                   WHERE source_mode = ? AND specialty = ? AND entity_type = ?""",
                (source_mode, specialty, entity_type),
            ).fetchone()
            client_id = max(start_at, int(row["maximum"] or (start_at - 1)) + 1)
            db.execute(
                """INSERT INTO client_entity_ids (
                    source_mode, specialty, entity_type, stable_key, client_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (source_mode, specialty, entity_type, stable_key, client_id, utc_now()),
            )
            return client_id

    def client_entity_ids_bulk(
        self,
        source_mode: str,
        specialty: str,
        entity_type: str,
        stable_keys: List[str],
        start_at: int = 1,
    ) -> Dict[str, int]:
        """Return stable client IDs for many keys using one database transaction."""
        ordered_keys = list(dict.fromkeys(str(key) for key in stable_keys))
        if not ordered_keys:
            return {}
        with self._lock, self.database() as db:
            rows = db.execute(
                """SELECT stable_key, client_id FROM client_entity_ids
                   WHERE source_mode = ? AND specialty = ? AND entity_type = ?""",
                (source_mode, specialty, entity_type),
            ).fetchall()
            result = {str(row["stable_key"]): int(row["client_id"]) for row in rows}
            next_id = max(
                start_at,
                max(result.values(), default=start_at - 1) + 1,
            )
            created_at = utc_now()
            additions = []
            for key in ordered_keys:
                if key in result:
                    continue
                result[key] = next_id
                additions.append(
                    (source_mode, specialty, entity_type, key, next_id, created_at)
                )
                next_id += 1
            if additions:
                db.executemany(
                    """INSERT INTO client_entity_ids (
                        source_mode, specialty, entity_type, stable_key, client_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    additions,
                )
        return {key: result[key] for key in ordered_keys}
