"""Network-free collector replay stress inside a dedicated QA container.

Only loopback HTTP is used. Source reports are mounted read-only, no credentials.
Never run against a production data directory.
"""
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import urllib.request
import zipfile

sys.path.insert(0, '/app')
from medik_pilot.storage import Storage, utc_now
from medik_pilot.collectors.reh2 import parse_report
from medik_pilot.domain import payload_status
from medik_pilot.comparison import compare_records, question_key, test_identity, write_report

ROOT = Path(os.environ['MEDIKTEST_DATA_DIR'])
if str(ROOT) != '/qa-data':
    raise SystemExit('QA data directory must be /qa-data')
store = Storage(ROOT/'pilot.db')
fixtures = json.loads(Path('/qa/fixtures.json').read_text())


def request(path, binary=False):
    with urllib.request.urlopen('http://127.0.0.1:8765'+path,timeout=60) as response:
        body=response.read()
    return body if binary else json.loads(body)


def replay(prefix, interrupt=False):
    runs=[]
    for fixture in fixtures:
        specialty, report, package = fixture['specialty'],fixture['report'],fixture['package']
        run_id = prefix+'-'+report['uid']
        if not store.get_run(run_id):
            store.create_run(run_id, dict(source_mode='live',test_source='reh2',specialty=specialty,
                material_type='test',document_mode='new',document_name='Offline QA '+specialty,
                reference_tests=0,reference_cases=0,verification_percent=0,max_attempts=1,
                max_requests=10000,max_duration_minutes=30,allow_create_attempts=False,
                allow_answer_submission=False,delay_seconds=0))
        items=parse_report(report,specialty,package,report['uid'],1)
        assert all(payload_status('test',i.payload)=='ready' for i in items)
        store.save_reh2_report(run_id,1,report)
        for index,item in enumerate(items):
            store.store_item(run_id,'live',specialty,item,1)
            if interrupt and index==len(items)//2:
                os._exit(23)  # real abrupt process exit after committed rows
        assert store.reh2_run_totals(run_id)['seen']==len(items)
        store.update_run(run_id,status='completed',finished_at=utc_now())
        runs.append(run_id)
    return runs


def snapshot():
    result={}
    for specialty in sorted({f['specialty'] for f in fixtures}):
        rows=store.comparison_snapshot(specialty)['items']
        ids=sorted([r['source_id'],store.client_entity_id('live',specialty,'test_question',r['source_id'])] for r in rows)
        identities=sorted(str(test_identity(r)) for r in rows)
        result[specialty]=dict(count=len(identities),ids=ids,
            identities_sha256=hashlib.sha256(json.dumps(identities).encode()).hexdigest())
    return result


def readers(runs):
    for run_id in runs:
        run=request('/api/runs/'+run_id)
        request('/api/health')
        data=request('/api/runs/'+run_id+'/export.json')
        assert data['run']['specialty']==run['specialty']
        with zipfile.ZipFile(io.BytesIO(request('/api/runs/'+run_id+'/export.xlsx',True))) as book:
            assert book.testzip() is None and 'xl/workbook.xml' in book.namelist()
        rows=store.comparison_snapshot(run['specialty'])['items']
        expected={test_identity(r) for r in rows}
        assert all(test_identity({'payload':r}) in expected for r in data['tests'])
        client={'headers':['question'],'skipped':0,'records':[
            {'key':question_key(r['payload']['question']),'line':i+2,'values':[r['payload']['question']]}
            for i,r in enumerate(rows)]}
        compared=compare_records(client,rows)
        assert not compared['missing_client'] and not compared['missing_parser']
        target=ROOT/('comparison-'+run_id+'.xlsx')
        write_report(target,client,compared,dict(specialty=run['specialty'],captured_at=utc_now(),filename='offline',sheet='Tests'))
        with zipfile.ZipFile(target) as report:
            assert report.testzip() is None


mode=sys.argv[1]
if mode=='interrupt':
    replay('crash-probe',True)
else:
    baseline_path=ROOT/'offline-baseline.json'
    runs=replay('baseline')
    if not baseline_path.exists():
        baseline_path.write_text(json.dumps(snapshot(),ensure_ascii=False))
    baseline=json.loads(baseline_path.read_text())
    if mode=='recover':
        replay('crash-probe')
    # Independent DB writers and panel process readers run concurrently.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures=[executor.submit(replay,'interrupted'),executor.submit(replay,'baseline'),executor.submit(readers,runs)]
        for future in futures: future.result()
    assert snapshot()==baseline, 'Counts, identities or client IDs changed'
    with store.database() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    print(json.dumps({'status':'passed','banks':{k:v['count'] for k,v in baseline.items()}},ensure_ascii=False))
