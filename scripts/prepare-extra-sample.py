import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
db = sqlite3.connect(ROOT / "data" / "pilot.db")
rows = db.execute(
    """select payload_json from items
       where source_mode='live' and specialty='Лечебное дело' and kind='test'
       order by id desc limit 30"""
).fetchall()
tests = []
seen = set()
for (payload_json,) in rows:
    item = json.loads(payload_json)
    if not item.get("question") or not any(o.get("is_correct") is True for o in item.get("options", [])):
        continue
    key = item["question"]
    if key in seen:
        continue
    seen.add(key)
    tests.append(item)
    if len(tests) == 10:
        break
case_row = db.execute(
    """select payload_json from items
       where source_mode='live' and specialty='Лечебное дело' and kind='case'
       order by id desc limit 1"""
).fetchone()
case = json.loads(case_row[0])
case["questions"] = case.get("questions", [])[:5]
out = {"specialty": "Лечебное дело", "tests": tests, "case": case}
(ROOT / "data" / "exports" / "extra-sample-input.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"tests": len(tests), "case_questions": len(case["questions"])}, ensure_ascii=False))
