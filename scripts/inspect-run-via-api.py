"""Print a compact run snapshot using unambiguous Unicode escapes."""

import argparse
import json
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    with urlopen(
        "{}/api/runs/{}".format(args.base_url.rstrip("/"), args.run_id),
        timeout=10,
    ) as response:
        run = json.load(response)
    fields = {
        key: run.get(key)
        for key in (
            "id",
            "status",
            "stop_reason",
            "attempts_completed",
            "items_seen",
            "unique_items",
            "duplicate_items",
            "changed_items",
            "requests_made",
            "error_count",
            "collected_by_kind",
            "error_message",
            "events",
        )
    }
    if args.compact:
        fields["latest_event"] = (run.get("events") or [None])[0]
        fields.pop("events", None)
    print(json.dumps(fields, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
