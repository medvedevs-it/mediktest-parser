import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify-migrated-server.py <database>")

    database = Path(sys.argv[1])
    if not database.is_file():
        raise SystemExit(f"database not found: {database}")

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {
            row["kind"]: row["count"]
            for row in connection.execute(
                """
                SELECT kind, COUNT(DISTINCT source_id) AS count
                FROM items
                WHERE status = 'ready'
                GROUP BY kind
                """
            )
        }
        incomplete_counts = {
            row["kind"]: row["count"]
            for row in connection.execute(
                """
                SELECT kind, COUNT(DISTINCT source_id) AS count
                FROM items
                WHERE status <> 'ready'
                GROUP BY kind
                """
            )
        }
        latest = connection.execute(
            """
            SELECT id, status, attempts_completed, items_seen, unique_items,
                   duplicate_items, changed_items, requests_made, stop_reason
            FROM runs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    result = {
        "database": str(database),
        "integrity": integrity,
        "tests": int(counts.get("test", 0)),
        "cases": int(counts.get("case", 0)),
        "incomplete_tests": int(incomplete_counts.get("test", 0)),
        "incomplete_cases": int(incomplete_counts.get("case", 0)),
        "latest_run": dict(latest) if latest else None,
    }
    print(json.dumps(result, ensure_ascii=False))

    if integrity != "ok" or result["tests"] < 5817 or result["cases"] < 556:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
