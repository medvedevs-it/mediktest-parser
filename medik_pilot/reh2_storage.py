"""Durable operation journal and source aliases; all question writes are atomic."""
import json
from dataclasses import replace
from datetime import datetime, timezone

from .domain import payload_status


class Reh2StorageMixin:
    def initialize_reh2(self, db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS reh2_operations (
                run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                state_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reh2_reports (
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                operation INTEGER NOT NULL, report_json TEXT NOT NULL,
                PRIMARY KEY(run_id, operation)
            );
            CREATE TABLE IF NOT EXISTS test_source_aliases (
                source_mode TEXT NOT NULL, specialty TEXT NOT NULL,
                provider TEXT NOT NULL, package TEXT NOT NULL, question_uid TEXT NOT NULL,
                source_id TEXT NOT NULL,
                PRIMARY KEY(source_mode, specialty, provider, package, question_uid)
            );
            CREATE TABLE IF NOT EXISTS reh2_receipts (
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                operation INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                outcome TEXT NOT NULL, diagnostic TEXT,
                PRIMARY KEY(run_id, operation, ordinal)
            );
        """)
        columns = {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
        if "test_source" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN test_source TEXT NOT NULL DEFAULT 'legacy'")

    def reh2_state(self, run_id):
        with self.database() as db:
            row = db.execute("SELECT state_json FROM reh2_operations WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_reh2_state(self, run_id, state):
        # Whitelist prevents accidental persistence of credentials or a session.
        allowed = {"account", "specialty", "package", "bank", "operation", "phase",
                   "history_before", "attempt_uid", "expected", "package_count", "no_new_packages", "ready_before"}
        if set(state) - allowed:
            raise ValueError("Unsupported REH2 checkpoint fields")
        with self._lock, self.database() as db:
            run = db.execute("SELECT specialty FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run or run[0] != state["specialty"]:
                raise ValueError("Checkpoint specialty mismatch")
            db.execute("INSERT INTO reh2_operations VALUES (?, ?) ON CONFLICT(run_id) "
                       "DO UPDATE SET state_json=excluded.state_json", (run_id, json.dumps(state, ensure_ascii=False)))

    def save_reh2_report(self, run_id, operation, report):
        with self._lock, self.database() as db:
            db.execute("INSERT INTO reh2_reports VALUES (?,?,?) ON CONFLICT(run_id,operation) DO NOTHING",
                       (run_id, operation, json.dumps(report, ensure_ascii=False)))

    def reh2_report(self, run_id, operation):
        with self.database() as db:
            row = db.execute("SELECT report_json FROM reh2_reports WHERE run_id=? AND operation=?",
                             (run_id, operation)).fetchone()
        return json.loads(row[0]) if row else None

    def reh2_progress(self, run_id):
        state = self.reh2_state(run_id)
        if not state:
            return None
        totals = self.reh2_package_stats(run_id)
        current = self.reh2_package_stats(run_id, state['operation'])
        return {"source": "reh2", "phase": state["phase"], "package": state["package"],
                "attempt_uid": state.get("attempt_uid"),
                "package_count": state.get("package_count", state.get("expected", 0)),
                "processed": current['received'], "repeats": totals['existing'],
                **totals, "current_package": current}

    def reh2_package_stats(self, run_id, operation=None):
        """Counters from durable receipts, never from raw insert outcomes alone.

        new_unique is relative to this run; existing means a ready record already
        in the bank at import. The two are independent (an existing bank question
        can be encountered for the first time in this run).
        """
        from .comparison import test_identity
        with self.database() as db:
            rows = db.execute("""SELECT r.operation,r.ordinal,r.outcome,r.diagnostic,
                i.status,i.payload_json FROM reh2_receipts r JOIN items i ON i.id=r.item_id
                WHERE r.run_id=? ORDER BY r.operation,r.ordinal""", (run_id,)).fetchall()
        counts = dict(received=0, ready=0, new_unique=0, existing=0, invalid=0)
        seen, diagnostics = set(), {}
        for row in rows:
            payload = json.loads(row['payload_json'])
            reason = row['diagnostic'] or payload.get('validation_error')
            ready = row['status'] == 'ready' and not reason
            key = test_identity({'payload': payload}) if ready else None
            fresh = ready and key not in seen
            if ready:
                seen.add(key)
            if operation is not None and row['operation'] != operation:
                continue
            counts['received'] += 1
            counts['ready'] += int(ready)
            counts['new_unique'] += int(fresh)
            counts['existing'] += int(ready and row['outcome'] == 'duplicate')
            counts['invalid'] += int(not ready)
            if not ready:
                reason = reason or 'Запись не прошла проверку готовности.'
                diagnostics[reason] = diagnostics.get(reason, 0) + 1
        return {**counts, 'diagnostics': [{'reason': k, 'count': v} for k, v in diagnostics.items()]}

    def reh2_attempt_was_processed(self, run_id, state):
        # Report UID remains available after the latest operation checkpoint moves
        # on. Scope to the same account, specialty and package; exclude resuming
        # the current operation, whose receipts must remain idempotent.
        with self.database() as db:
            rows = db.execute("""SELECT p.run_id,p.operation,p.report_json,o.state_json
                FROM reh2_reports p JOIN reh2_operations o ON o.run_id=p.run_id
                JOIN runs r ON r.id=p.run_id WHERE r.specialty=?
                AND NOT (p.run_id=? AND p.operation=?)
                AND EXISTS (SELECT 1 FROM reh2_receipts c
                    WHERE c.run_id=p.run_id AND c.operation=p.operation)""",
                (state['specialty'], run_id, state['operation'])).fetchall()
        for row in rows:
            prior = json.loads(row['state_json'])
            if prior['account'] == state['account'] and prior['package'] == state['package']:
                if json.loads(row['report_json']).get('uid') == state['attempt_uid']:
                    return True
        return False

    def finish_reh2_package(self, run_id):
        state = self.reh2_state(run_id)
        if not state:
            raise ValueError("Missing REH2 operation")
        if state["phase"] == "committed":
            return state
        with self.database() as db:
            count = db.execute("SELECT COUNT(*) FROM reh2_receipts WHERE run_id=? AND operation=?",
                               (run_id, state["operation"])).fetchone()[0]
        if count != state.get("package_count"):
            raise ValueError("REH2: пакет не сохранён полностью; следующая попытка запрещена.")
        ready = len(self.run_ready_identity_keys(run_id)["test"])
        state["no_new_packages"] = state.get("no_new_packages", 0) + 1 if ready == state.get("ready_before", 0) else 0
        state["phase"] = "committed"
        self.save_reh2_state(run_id, state)
        return state

    def reh2_store_item(self, run_id, source_mode, specialty, item, attempt_number):
        from .comparison import test_identity
        meta = item.raw_payload["_reh2"]
        if source_mode != "live" or meta["specialty"] != specialty:
            raise ValueError("REH2 specialty/source mismatch")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.database() as db:
            # Reserve the write transaction before reading aliases and receipts.
            db.execute("BEGIN IMMEDIATE")
            receipt = db.execute("SELECT item_id FROM reh2_receipts WHERE run_id=? AND operation=? AND ordinal=?",
                                 (run_id, meta["operation"], meta["index"])).fetchone()
            if receipt:
                return "replayed", int(receipt[0])
            diagnostic = item.payload.get("validation_error")
            if not diagnostic:
                alias_key = (source_mode, specialty, "reh2", meta["package"], meta["question_uid"])
                alias = db.execute("SELECT source_id FROM test_source_aliases WHERE source_mode=? "
                                   "AND specialty=? AND provider=? AND package=? AND question_uid=?", alias_key).fetchone()
                if alias:
                    item = replace(item, source_id=alias[0])
                else:
                    candidates = db.execute("""SELECT i.source_id,i.payload_json FROM items i
                        WHERE i.source_mode=? AND i.specialty=? AND i.kind='test' AND i.status='ready'
                        AND i.version=(SELECT MAX(j.version) FROM items j WHERE j.source_mode=i.source_mode
                        AND j.specialty=i.specialty AND j.kind=i.kind AND j.source_id=i.source_id)""",
                        (source_mode, specialty)).fetchall()
                    identity = test_identity({"payload": item.payload})
                    matches = [r for r in candidates if test_identity({"payload": json.loads(r["payload_json"])}) == identity]
                    if len(matches) > 1:
                        diagnostic = "Неоднозначное совпадение со старым банком: {} записей. Автоматическое объединение отключено.".format(len(matches))
                        item = replace(item, payload={**item.payload, "validation_error": diagnostic})
                    else:
                        if matches:
                            item = replace(item, source_id=matches[0]["source_id"])
                        db.execute("INSERT INTO test_source_aliases VALUES (?,?,?,?,?,?)", (*alias_key, item.source_id))
            previous = db.execute("SELECT * FROM items WHERE source_mode=? AND specialty=? "
                                  "AND kind='test' AND source_id=? ORDER BY version DESC LIMIT 1",
                                  (source_mode, specialty, item.source_id)).fetchone()
            same = previous and not diagnostic and previous["status"] == "ready" and (
                test_identity({"payload": json.loads(previous["payload_json"])}) == test_identity({"payload": item.payload}))
            if previous and (same or previous["content_hash"] == item.content_hash):
                outcome, item_id = "duplicate", previous["id"]
                db.execute("UPDATE items SET last_seen_at=? WHERE id=?", (now, item_id))
            else:
                outcome = "changed" if previous else "new"
                cursor = db.execute("""INSERT INTO items(source_mode,specialty,kind,source_id,content_hash,
                    version,payload_json,raw_payload_json,status,first_seen_at,version_created_at,last_seen_at)
                    VALUES (?,?,'test',?,?,?,?,?,?,?,?,?)""", (
                    source_mode, specialty, item.source_id, item.content_hash,
                    previous["version"] + 1 if previous else 1,
                    json.dumps(item.payload, ensure_ascii=False), json.dumps(item.raw_payload, ensure_ascii=False),
                    payload_status("test", item.payload), previous["first_seen_at"] if previous else now, now, now))
                item_id = cursor.lastrowid
            db.execute("INSERT INTO run_items(run_id,item_id,attempt_number,outcome,seen_at) VALUES (?,?,?,?,?)",
                       (run_id, item_id, attempt_number, outcome, now))
            db.execute("INSERT INTO reh2_receipts VALUES (?,?,?,?,?,?)",
                       (run_id, meta["operation"], meta["index"], item_id, outcome, diagnostic))
            # Durable receipt, bank version, run link and counters commit together.
            db.execute("UPDATE runs SET items_seen=items_seen+1, requests_made=requests_made+1, "
                       "unique_items=unique_items+?, duplicate_items=duplicate_items+?, changed_items=changed_items+? WHERE id=?",
                       (int(outcome == "new"), int(outcome == "duplicate"), int(outcome == "changed"), run_id))
        return outcome, int(item_id)

    def reh2_run_totals(self, run_id):
        with self.database() as db:
            rows = db.execute("SELECT outcome,COUNT(*) AS n FROM run_items WHERE run_id=? GROUP BY outcome",
                              (run_id,)).fetchall()
        counts = {r["outcome"]: r["n"] for r in rows}
        total = sum(counts.values())
        return {"seen": total, "requests": total, "new": counts.get("new", 0),
                "duplicate": counts.get("duplicate", 0), "changed": counts.get("changed", 0)}

    def stored_item(self, item_id):
        with self.database() as db:
            row = db.execute("SELECT kind,source_id,payload_json,status FROM items WHERE id=?", (item_id,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload_json"])}

    def reh2_verification_seen(self, run_id, target):
        """Rebuild verification progress from committed links, not a stale UI checkpoint."""
        from .comparison import test_identity
        with self.database() as db:
            rows = db.execute("SELECT i.payload_json FROM run_items r JOIN items i ON i.id=r.item_id "
                              "WHERE r.run_id=? AND i.kind='test' AND i.status='ready' ORDER BY r.id", (run_id,)).fetchall()
        unique, checked = set(), 0
        for row in rows:
            if len(unique) >= target:
                checked += 1
            unique.add(test_identity({"payload": json.loads(row[0])}))
        return checked
