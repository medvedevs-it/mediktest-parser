"""Extract a small private QA fixture from read-only historical databases."""
import argparse
import json
from pathlib import Path
import sqlite3

parser = argparse.ArgumentParser()
parser.add_argument('--pediatrics', required=True)
parser.add_argument('--general', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
target = Path(args.output)
if target.exists():
    raise SystemExit('Refusing to overwrite fixtures')
fixtures = []
for source, specialty, package in (
    (args.pediatrics, 'Педиатрия', 'PediatricsSpec_2026_v11'),
    (args.general, 'Лечебное дело', 'GeneralMedicine_2026_v11'),
):
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro', uri=True) as db:
        rows = db.execute('SELECT report_json FROM reh2_reports ORDER BY rowid').fetchall()
    for row in (rows[0], rows[-1]):
        report = json.loads(row[0])
        fixtures.append(dict(specialty=specialty, package=package,
                             report={key:report[key] for key in ('uid','statistics','archived') if key in report}))
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(fixtures, ensure_ascii=False), encoding='utf-8')
print('Saved', len(fixtures), 'private reports')
