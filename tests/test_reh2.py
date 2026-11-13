import copy
import html
import io
import json
import tempfile
import unittest
import uuid
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from medik_pilot.collectors.reh2 import (
    Reh2Api, Reh2Collector, Reh2Error, Reh2SourceExhausted, Reh2TransientError, CollectionInterrupted,
    GROUPS, CODES, parse_report, verify_bank,
)
from medik_pilot.domain import CollectedItem, payload_status, normalize_text
from medik_pilot.specialties import PACKAGE_TITLES
from medik_pilot.storage import Storage
from medik_pilot.runner import RunManager


GENERAL, PEDIATRICS = "Лечебное дело", "Педиатрия"


def question(uid=None, text="ВОПРОС С 10<sup>9</sup>?"):
    return {"uid": uid or str(uuid.uuid4()), "questionText": text,
            "answers": [{"text": "Ответ " + str(i), "correct": i == 0} for i in range(4)]}


def bank(specialty):
    return {"uid": str(uuid.uuid4()), "packageName": PACKAGE_TITLES[specialty],
            "packageIdGroup": GROUPS[specialty], "packageId": GROUPS[specialty] + "_v11",
            "version": 11, "speciality": {"code": CODES[specialty]}}


def report(specialty, questions, uid):
    chunks = ['<p>Банк тестовых заданий: ' + PACKAGE_TITLES[specialty] + '</p>']
    for i, q in enumerate(questions):
        chunks.append('<h3>Вопрос <span>{}</span></h3><p class="question-uid"><span>{}</span></p>'.format(
            i + 1, html.escape(json.dumps(q, ensure_ascii=False))))
    return {"uid": uid, "statistics": '<html><body>' + ''.join(chunks) + '</body></html>'}


def config(specialty=GENERAL, **extra):
    return dict(source_mode="live", test_source="reh2", specialty=specialty, material_type="test",
                max_attempts=5, reference_tests=3, reference_cases=0, verification_percent=0,
                delay_seconds=0, max_duration_minutes=5, max_requests=1000,
                allow_create_attempts=True, allow_answer_submission=True, **extra)


class FakeApi:
    def __init__(self, specialty, repeated=False):
        self.bank = bank(specialty)
        self.specialty = specialty
        self.created = []
        self.create_calls = self.finish_calls = 0
        self.fail_create = self.fail_finish = self.fail_report = False
        self.repeated = repeated
        self.questions = [question(text='ВОПРОС '+str(i)) for i in range(5)]

    def banks(self):
        return [self.bank]

    def history(self):
        return copy.deepcopy(self.created)

    def create(self, bank):
        self.create_calls += 1
        uid = str(uuid.uuid4())
        qs = self.questions if self.repeated else [question(text='ВОПРОС {} {}'.format(self.create_calls, i)) for i in range(5)]
        self.created.append({"uid": uid, "bank": self.bank, "timeFinished": None, "questions": qs})
        if self.fail_create:
            self.fail_create = False
            raise Reh2TransientError("lost create response")
        return uid

    def attempt(self, uid):
        return copy.deepcopy(next(r for r in self.created if r["uid"] == uid))

    def finish(self, uid):
        self.finish_calls += 1
        next(r for r in self.created if r["uid"] == uid)["timeFinished"] = "finished"
        if self.fail_finish:
            self.fail_finish = False
            raise Reh2TransientError("lost finish response")

    def report(self, uid):
        if self.fail_report:
            self.fail_report = False
            raise Reh2TransientError("report unavailable")
        return report(self.specialty, self.attempt(uid)["questions"], uid)


class Reh2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(Path(self.tmp.name) / 'isolated.db')
        self.storage.create_run('run', config())

    def collector(self, api=None, username="account", run='run', specialty=GENERAL):
        return Reh2Collector(SimpleNamespace(username=username, password='not-persisted'), specialty,
                             self.storage, run, Mock(), True, True, api or FakeApi(specialty))

    def parse(self, q, specialty=GENERAL, index=1):
        uid = str(uuid.uuid4())
        return parse_report(report(specialty, [q], uid), specialty, GROUPS[specialty]+'_v11', uid, index, 1)[0]

    def test_parser_both_specialties_and_raw(self):
        for specialty in (GENERAL, PEDIATRICS):
            q = question()
            item = self.parse(q, specialty)
            self.assertEqual(payload_status('test', item.payload), 'ready')
            self.assertIn('10⁹', item.payload['question'])
            self.assertTrue(item.payload['question'].startswith('Вопрос'))
            self.assertEqual(item.raw_payload['question'], q)
            self.assertEqual(sum(o['is_correct'] for o in item.payload['options']), 1)

    def test_numeric_comparison_in_real_answer_is_not_markup(self):
        formula = '<70 mmHg + [возраст ребенка в годах × 2], mmHg'
        for specialty in (GENERAL, PEDIATRICS):
            q = question(text='<p>ДЛЯ ОПРЕДЕЛЕНИЯ ГИПОТЕНЗИИ У ДЕТЕЙ</p>')
            q['answers'][0]['text'] = '<p>' + formula + '</p>'
            item = self.parse(q, specialty)
            self.assertEqual(payload_status('test', item.payload), 'ready')
            self.assertEqual(item.payload['options'][0]['text'], formula)
            self.assertTrue(item.payload['options'][0]['is_correct'])
            self.assertEqual(item.raw_payload['question'], q)

    def test_numeric_comparisons_entities_and_powers_survive_cleanup(self):
        samples = {
            '<p><70 mmHg</p>': '<70 mmHg',
            '<p>&lt;70 mmHg</p>': '<70 mmHg',
            '<p>p<0,05 и >0,01</p>': 'p<0,05 и >0,01',
            '<b>3 < 5; 7 > 2; <=10; >=2</b>': '3 < 5; 7 > 2; <=10; >=2',
            '<p>10<sup><b>9</b></sup>/л<br/>H<sub>2</sub>O</p>': '10⁹/л H₂O',
            '<span title="a > b">&lt;70</span>': '<70',
        }
        for raw, expected in samples.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_text(raw), expected)
                self.assertEqual(normalize_text(expected), expected)

    def test_invalid_records_accounted_not_ready(self):
        for mutate in (lambda q:q['answers'].pop(), lambda q:q['answers'][1].update(correct=True),
                       lambda q:q.update(questionText=''), lambda q:q['answers'][1].update(correct=None),
                       lambda q:q.update(uid='bad')):
            q = question()
            mutate(q)
            item = self.parse(q)
            self.assertEqual(payload_status('test', item.payload), 'invalid')
            self.assertTrue(item.payload['validation_error'])

    def test_wrong_specialty_and_count_rejected(self):
        uid = str(uuid.uuid4())
        for specialty, count in ((PEDIATRICS,1), (GENERAL,2)):
            with self.assertRaises(Reh2Error):
                parse_report(report(GENERAL,[question()],uid),specialty,'package',uid,1,count)
        b = bank(GENERAL)
        with self.assertRaises(Reh2Error): verify_bank(b, PEDIATRICS)
        b['packageIdGroup'] = 'GeneralMedicine_2025'
        with self.assertRaises(Reh2Error): verify_bank(b, GENERAL)

    def test_legacy_match_preserves_id_and_ignores_answer_order(self):
        q = question()
        item = self.parse(q)
        old = CollectedItem('test','legacy-id',item.payload)
        self.storage.store_item('run','live',GENERAL,old,0)
        client_id = self.storage.client_entity_id('live',GENERAL,'test_question','legacy-id')
        q['answers'].reverse()
        item = self.parse(q)
        outcome, item_id = self.storage.store_item('run','live',GENERAL,item,1)
        self.assertEqual(outcome,'duplicate')
        self.assertEqual(self.storage.stored_item(item_id)['source_id'],'legacy-id')
        self.assertEqual(self.storage.client_entity_id('live',GENERAL,'test_question','legacy-id'),client_id)
        self.assertEqual(self.storage.store_item('run','live',GENERAL,item,1)[0], 'replayed')
        self.assertEqual(len(self.storage.run_ready_identity_keys('run')['test']), 1)

    def test_changed_alias_versions_without_reassigning_client_id(self):
        q = question()
        item = self.parse(q)
        _, first = self.storage.store_item('run','live',GENERAL,item,1)
        source_id = self.storage.stored_item(first)['source_id']
        stable = self.storage.client_entity_id('live',GENERAL,'test_question',source_id)
        q['answers'][0]['text'] = 'Другой правильный ответ'
        changed = self.parse(q,index=2)
        outcome, second = self.storage.store_item('run','live',GENERAL,changed,2)
        self.assertEqual(outcome,'changed')
        self.assertNotEqual(first,second)
        self.assertEqual(self.storage.stored_item(second)['source_id'],source_id)
        self.assertEqual(self.storage.client_entity_id('live',GENERAL,'test_question',source_id),stable)

    def test_same_uuid_and_text_do_not_cross_specialties_and_reset(self):
        self.storage.create_run('ped',config(PEDIATRICS))
        q = question()
        for run, specialty in (('run',GENERAL),('ped',PEDIATRICS)):
            item = self.parse(q,specialty)
            self.storage.store_item(run,'live',specialty,item,1)
            self.assertEqual(self.storage.client_entity_id('live',specialty,'test_question',item.source_id),1)
        self.storage.reset_specialty_materials(GENERAL)
        self.assertEqual(len(self.storage.current_bank_rows(GENERAL)),0)
        self.assertEqual(len(self.storage.current_bank_rows(PEDIATRICS)),1)
        self.assertIsNotNone(self.storage.get_run('ped'))
        with self.storage.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM test_source_aliases').fetchone()[0],1)

    def test_failed_db_transaction_rolls_back_alias_and_receipt(self):
        with self.storage.database() as db:
            db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON reh2_receipts BEGIN SELECT RAISE(ABORT,'injected'); END")
        item = self.parse(question())
        with self.assertRaises(Exception): self.storage.store_item('run','live',GENERAL,item,1)
        with self.storage.database() as db:
            for table in ('items','test_source_aliases','reh2_receipts','run_items'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_lost_creation_response_recovers_without_second_attempt(self):
        api = FakeApi(GENERAL)
        api.fail_create = True
        c = self.collector(api)
        with self.assertRaises(Reh2TransientError): c.collect_attempt('test',1)
        c = self.collector(api)
        self.assertEqual(len(c.collect_attempt('test',1)),5)
        self.assertEqual(api.create_calls,1)

    def test_lost_finish_and_report_response_recover(self):
        for phase in ('fail_finish','fail_report'):
            with self.subTest(phase=phase):
                run = phase
                self.storage.create_run(run,config())
                api = FakeApi(GENERAL)
                setattr(api,phase,True)
                c = self.collector(api,run=run)
                with self.assertRaises(Reh2TransientError): c.collect_attempt('test',1)
                self.assertEqual(len(self.collector(api,run=run).collect_attempt('test',1)),5)
                self.assertEqual(api.create_calls,1)
                self.assertEqual(api.finish_calls,1)

    def test_partial_import_restart_and_whole_package_gate(self):
        api = FakeApi(GENERAL)
        c = self.collector(api)
        items = c.collect_attempt('test',1)
        self.storage.store_item('run','live',GENERAL,items[0],1)
        with self.assertRaises(ValueError): c.acknowledge_attempt('test')
        resumed = self.collector(api)
        restored = resumed.collect_attempt('test',2)
        self.assertEqual(api.create_calls,1)
        for item in restored: self.storage.store_item('run','live',GENERAL,item,1)
        resumed.acknowledge_attempt('test')
        self.assertEqual(len(self.storage.run_ready_identity_keys('run')['test']),5)
        self.assertEqual(self.storage.reh2_progress('run')['processed'],5)
        resumed.collect_attempt('test',2)
        self.assertEqual(api.create_calls,2)

    def test_changed_account_and_ambiguous_recovery_stop(self):
        api = FakeApi(GENERAL)
        api.fail_create = True
        with self.assertRaises(Reh2TransientError): self.collector(api).collect_attempt('test',1)
        with self.assertRaises(Reh2Error): self.collector(api,username='different').collect_attempt('test',1)
        api.create(api.bank)
        with self.assertRaises(Reh2Error): self.collector(api).collect_attempt('test',1)
        self.assertEqual(api.create_calls,2)

    def test_pause_and_stop_do_not_start_external_attempt(self):
        api = FakeApi(GENERAL)
        c = self.collector(api)
        c.pause_callback = Mock()
        c.stop_callback = lambda: True
        with self.assertRaises(CollectionInterrupted): c.collect_attempt('test',1)
        c.pause_callback.assert_called_once()
        self.assertEqual(api.create_calls,0)

    def test_checkpoint_never_contains_password(self):
        self.collector().collect_attempt('test',1)
        self.assertNotIn('not-persisted',json.dumps(self.storage.reh2_state('run')))
        state = self.storage.reh2_state('run')
        state['password']='secret'
        with self.assertRaises(ValueError): self.storage.save_reh2_state('run',state)

    def test_runner_saves_whole_package_and_counts_ready(self):
        manager = RunManager(self.storage)
        api = FakeApi(GENERAL)
        c = self.collector(api)
        with patch.object(manager,'_collector',return_value=c): manager._execute('run',config())
        result = self.storage.get_run('run')
        self.assertEqual(result['status'],'completed',result.get('error_message'))
        self.assertEqual(result['collected_by_kind']['test'],5)
        self.assertEqual(api.create_calls,1)

    def test_duplicate_packages_continue_until_attempt_limit(self):
        cfg = config()
        cfg['reference_tests']=100
        manager=RunManager(self.storage)
        api=FakeApi(GENERAL,repeated=True)
        with patch.object(manager,'_collector',return_value=self.collector(api)):
            manager._execute('run',cfg)
        result=self.storage.get_run('run')
        self.assertEqual(result['status'],'partial',result.get('error_message'))
        self.assertEqual(result['stop_reason'],'max_attempts')
        self.assertEqual(api.create_calls,5)
        self.assertEqual(result['collected_by_kind']['test'],5)

    def test_three_duplicate_packages_then_new_package_both_specialties(self):
        for specialty in (GENERAL, PEDIATRICS):
            with self.subTest(specialty=specialty):
                run = specialty
                cfg = config(specialty); cfg.update(reference_tests=10, max_attempts=6)
                self.storage.create_run(run, cfg)
                api = FakeApi(specialty, repeated=True)
                original = api.create
                def create(b):
                    api.repeated = api.create_calls < 4
                    return original(b)
                api.create = create
                manager = RunManager(self.storage)
                with patch.object(manager, '_collector', return_value=self.collector(api, run=run, specialty=specialty)):
                    manager._execute(run, cfg)
                result = self.storage.get_run(run)
                self.assertEqual(result['status'], 'completed', result.get('error_message'))
                self.assertEqual(api.create_calls, 5)
                self.assertEqual(result['collected_by_kind']['test'], 10)

    def test_ambiguous_legacy_package_exposes_invalid_not_new_ready(self):
        # Reproduce the screenshot symptom without claiming these are client data.
        self.storage.create_run('old', config())
        q = question(); item = self.parse(q)
        for identity in ('old1', 'old2'):
            payload = copy.deepcopy(item.payload)
            if identity == 'old2': payload['options'].reverse()
            self.storage.store_item('old', 'live', GENERAL, CollectedItem('test', identity, payload), 0)
        api = FakeApi(GENERAL, repeated=True); api.questions = [q]
        manager = RunManager(self.storage)
        with patch.object(manager, '_collector', return_value=self.collector(api)):
            manager._execute('run', config())
        result = self.storage.get_run('run')
        self.assertEqual(result['stop_reason'], 'max_attempts')
        self.assertEqual(result['test_progress']['received'], 5)
        self.assertEqual(result['test_progress']['ready'], 0)
        self.assertEqual(result['test_progress']['invalid'], 5)
        self.assertEqual(result['ready_outcomes'].get('new', 0), 0)
        self.assertTrue(any('Неоднозначное совпадение' in e['message'] for e in result['events']))
        self.assertFalse(any('Тесты: новых' in e['message'] for e in result['events']))

    def test_reused_attempt_uid_stops_before_finishing_or_accounting(self):
        api = FakeApi(GENERAL)
        c = self.collector(api)
        for item in c.collect_attempt('test', 1):
            self.storage.store_item('run', 'live', GENERAL, item, 1)
        c.acknowledge_attempt('test')
        uid = api.created[0]['uid']
        api.create = Mock(return_value=uid)
        with self.assertRaisesRegex(Reh2Error, 'повторно вернул'):
            c.collect_attempt('test', 2)
        with self.assertRaisesRegex(Reh2Error, 'повторно вернул'):
            self.collector(api).collect_attempt('test', 2)
        api.create.assert_called_once()
        self.assertEqual(api.finish_calls, 1)
        self.assertEqual(self.storage.reh2_run_totals('run')['seen'], 5)

    def test_package_counts_survive_replay_and_partial_import(self):
        c = self.collector()
        items = c.collect_attempt('test', 1)
        self.storage.store_item('run', 'live', GENERAL, items[0], 1)
        for item in items:
            self.storage.store_item('run', 'live', GENERAL, item, 1)
        c.acknowledge_attempt('test')
        stats = self.storage.reh2_package_stats('run', 1)
        self.assertEqual({k: stats[k] for k in ('received','ready','new_unique','existing','invalid')},
                         dict(received=5, ready=5, new_unique=5, existing=0, invalid=0))
        for item in items: self.storage.store_item('run', 'live', GENERAL, item, 1)
        self.assertEqual(self.storage.reh2_package_stats('run', 1), stats)

    def test_old_bank_matches_count_as_ready_unique_in_new_run(self):
        self.storage.create_run('old', config())
        api = FakeApi(GENERAL, repeated=True)
        for index, q in enumerate(api.questions):
            self.storage.store_item('old', 'live', GENERAL,
                CollectedItem('test', 'old'+str(index), self.parse(q).payload), 0)
        manager = RunManager(self.storage)
        with patch.object(manager, '_collector', return_value=self.collector(api)):
            manager._execute('run', config())
        result = self.storage.get_run('run')
        self.assertEqual(result['status'], 'completed')
        stats = self.storage.reh2_package_stats('run', 1)
        self.assertEqual(stats['existing'], 5)
        self.assertEqual(stats['new_unique'], 5)
        self.assertEqual(result['collected_by_kind']['test'], 5)

    def test_repeated_uid_guard_survives_history_omission_and_new_run(self):
        api = FakeApi(GENERAL); c = self.collector(api)
        for item in c.collect_attempt('test', 1):
            self.storage.store_item('run', 'live', GENERAL, item, 1)
        c.acknowledge_attempt('test')
        self.storage.create_run('next', config())
        api.history = Mock(return_value=[])
        api.create = Mock(return_value=api.created[0]['uid'])
        manager = RunManager(self.storage)
        with patch.object(manager, '_collector', return_value=self.collector(api, run='next')):
            manager._execute('next', config())
        result = self.storage.get_run('next')
        self.assertEqual(result['stop_reason'], 'repeated_attempt')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['items_seen'], 0)
        self.assertEqual(api.finish_calls, 1)
        manager.set_credentials('account', 'not-persisted')
        with self.assertRaisesRegex(RuntimeError, 'повторно вернул'): manager.resume('next')

    def test_request_time_and_stop_limits_still_apply(self):
        for limit in ('max_requests', 'max_duration', 'user'):
            with self.subTest(limit=limit):
                self.storage.create_run(limit, config())
                manager = RunManager(self.storage); api = FakeApi(GENERAL)
                cfg = config(); cfg['reference_tests'] = 100
                if limit == 'max_requests': cfg['max_requests'] = 2
                if limit == 'max_duration': cfg['max_duration_minutes'] = 0
                if limit == 'user': manager._stop_event.set()
                with patch.object(manager, '_collector', return_value=self.collector(api, run=limit)):
                    manager._execute(limit, cfg)
                result = self.storage.get_run(limit)
                self.assertEqual(result['stop_reason'], limit)
                self.assertEqual(result['items_seen'], 2 if limit == 'max_requests' else 0)

    def test_old_no_new_packages_run_not_auto_resumed(self):
        self.storage.update_run('run', status='partial', stop_reason='no_new_packages')
        manager = RunManager(self.storage)
        manager.set_credentials('account', 'not-persisted')
        with self.assertRaises(RuntimeError): manager.resume('run')
        self.assertEqual(self.storage.get_run('run')['stop_reason'], 'no_new_packages')

    def test_concurrent_progress_comparison_and_idempotent_import(self):
        item=self.parse(question())
        def worker(i):
            if i % 3 == 0: return self.storage.store_item('run','live',GENERAL,item,1)
            if i % 3 == 1: return self.storage.get_run('run')
            return self.storage.comparison_snapshot(GENERAL)
        with ThreadPoolExecutor(max_workers=6) as pool: list(pool.map(worker,range(36)))
        self.assertEqual(len(self.storage.run_ready_identity_keys('run')['test']),1)
        self.assertEqual(self.storage.reh2_run_totals('run')['seen'],1)

    def test_run_source_is_persisted_and_legacy_default(self):
        self.assertEqual(self.storage.run_config('run')['test_source'],'reh2')
        cfg=config();cfg.pop('test_source')
        self.storage.create_run('old',cfg)
        self.assertEqual(self.storage.run_config('old')['test_source'],'legacy')

    def test_cases_still_use_legacy(self):
        c=self.collector()
        c.collect_attempt('case',1)
        c.legacy.collect_attempt.assert_called_once_with('case',1)

    def test_different_answers_are_not_matched_to_legacy(self):
        q=question(); item=self.parse(q)
        self.storage.store_item('run','live',GENERAL,CollectedItem('test','old',item.payload),0)
        q['answers'][0]['text']='Изменённый ответ'
        outcome, item_id=self.storage.store_item('run','live',GENERAL,self.parse(q),1)
        self.assertEqual(outcome,'new')
        self.assertNotEqual(self.storage.stored_item(item_id)['source_id'],'old')

    def test_ambiguous_old_records_are_diagnostic_not_silently_merged(self):
        q=question(); item=self.parse(q)
        self.storage.store_item('run','live',GENERAL,CollectedItem('test','old1',item.payload),0)
        payload=copy.deepcopy(item.payload); payload['options'].reverse()
        self.storage.store_item('run','live',GENERAL,CollectedItem('test','old2',payload),0)
        _, item_id=self.storage.store_item('run','live',GENERAL,item,1)
        self.assertEqual(self.storage.stored_item(item_id)['status'],'invalid')
        with self.storage.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM test_source_aliases').fetchone()[0],0)

    def test_uncertain_create_with_no_candidate_never_creates_another(self):
        api=FakeApi(GENERAL);api.fail_create=True
        with self.assertRaises(Reh2TransientError): self.collector(api).collect_attempt('test',1)
        api.created=[]
        with self.assertRaises(Reh2Error): self.collector(api).collect_attempt('test',1)
        self.assertEqual(api.create_calls,1)

    def test_acknowledgement_replay_does_not_increment_empty_streak(self):
        c=self.collector()
        for item in c.collect_attempt('test',1): self.storage.store_item('run','live',GENERAL,item,1)
        a=c.acknowledge_attempt('test')
        b=c.acknowledge_attempt('test')
        self.assertEqual(a,b)
        self.assertEqual(b['no_new_packages'],0)

    def test_backup_includes_committed_wal_records(self):
        import sqlite3
        from contextlib import closing
        item=self.parse(question())
        self.storage.store_item('run','live',GENERAL,item,1)
        target=Path(self.tmp.name)/'backup.db'
        self.storage.backup(target)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reh2_receipts').fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_json_and_catalog_new_exports_remain_specialty_scoped(self):
        from medik_pilot.exporter import export_json
        self.storage.create_run('ped',config(PEDIATRICS))
        for run,specialty in (('run',GENERAL),('ped',PEDIATRICS)):
            self.storage.store_item(run,'live',specialty,self.parse(question(),specialty),1)
        with patch('medik_pilot.exporter.EXPORT_DIR',Path(self.tmp.name)):
            for run,specialty in (('run',GENERAL),('ped',PEDIATRICS)):
                content=json.loads(export_json(self.storage,run).read_text(encoding='utf-8'))
                self.assertEqual(len(content['tests']),1)
                self.assertEqual(content['run']['specialty'],specialty)
                self.assertEqual(content['run']['test_source'],'reh2')
                self.assertEqual(len(self.storage.export_rows(run)),1)
                self.assertEqual(len(self.storage.catalog_rows_for_run(run)),1)

    def test_permanent_failure_does_not_repeat(self):
        manager=RunManager(self.storage)
        collector=self.collector()
        collector.api.banks=Mock(side_effect=Reh2Error('missing package'))
        with self.assertRaises(Reh2Error): manager._collect_with_retry(collector,'run','test',1)
        collector.api.banks.assert_called_once()

    def test_restart_after_target_reached_still_finishes_pending_package(self):
        api=FakeApi(GENERAL)
        c=self.collector(api)
        items=c.collect_attempt('test',1)
        for item in items[:3]: self.storage.store_item('run','live',GENERAL,item,1)
        self.assertEqual(len(self.storage.run_ready_identity_keys('run')['test']),3)
        manager=RunManager(self.storage)
        with patch.object(manager,'_collector',return_value=self.collector(api)):
            manager._execute('run',config())
        result=self.storage.get_run('run')
        self.assertEqual(result['status'],'completed',result.get('error_message'))
        self.assertEqual(result['collected_by_kind']['test'],5)
        self.assertEqual(api.create_calls,1)
        self.assertEqual(self.storage.reh2_state('run')['phase'],'committed')

    def test_verification_recovers_after_receipts_before_ui_checkpoint(self):
        api=FakeApi(GENERAL)
        c=self.collector(api)
        for item in c.collect_attempt('test',1): self.storage.store_item('run','live',GENERAL,item,1)
        self.assertEqual(self.storage.reh2_verification_seen('run',3),2)
        cfg=config();cfg['verification_percent']=50
        manager=RunManager(self.storage)
        with patch.object(manager,'_collector',return_value=self.collector(api)):
            manager._execute('run',cfg)
        self.assertEqual(api.create_calls,1)
        self.assertEqual(self.storage.get_run('run')['status'],'completed')

    def test_http409_only_verified_creation_message_means_exhausted(self):
        message = 'Закончились вопросы по данной дисциплине. Необходимо очистить историю'
        for path, body, expected in (
            ('/quiz/start-attempt', json.dumps({'message': message}).encode(), Reh2SourceExhausted),
            ('/quiz/start-attempt', b'{"message":"unknown secret detail"}', Reh2Error),
            ('/quiz/start-attempt', b'<html>error</html>', Reh2Error),
            ('/quiz/finish-attempt/example', json.dumps({'message': message}).encode(), Reh2Error),
        ):
            with self.subTest(path=path, body=body):
                api = Reh2Api('user', 'password')
                api.token = str(uuid.uuid4())
                error = urllib.error.HTTPError(api.BASE + path, 409, 'Conflict', {}, io.BytesIO(body))
                with patch('urllib.request.urlopen', side_effect=error) as request:
                    with self.assertRaises(expected) as caught:
                        api.request(path)
                self.assertIs(type(caught.exception), expected)
                self.assertNotIn('secret', str(caught.exception))
                request.assert_called_once()

    def test_exhaustion_checkpoint_prevents_repeated_creation(self):
        api = FakeApi(GENERAL)
        api.create = Mock(side_effect=Reh2SourceExhausted())
        c = self.collector(api)
        for number in (1, 1, 2):
            with self.assertRaises(Reh2SourceExhausted):
                c.collect_attempt('test', number)
        self.assertEqual(self.storage.reh2_state('run')['phase'], 'exhausted')
        api.create.assert_called_once()
        manager = RunManager(self.storage)
        with self.assertRaisesRegex(RuntimeError, 'истории'):
            manager.resume('run')

    def test_exhaustion_is_partial_and_preserves_saved_package_and_export(self):
        from medik_pilot.exporter import export_json
        api = FakeApi(GENERAL)
        original_create = api.create
        def create_once(bank):
            if api.created:
                raise Reh2SourceExhausted()
            return original_create(bank)
        api.create = Mock(side_effect=create_once)
        cfg = config()
        cfg['reference_tests'] = 100
        manager = RunManager(self.storage)
        with patch.object(manager, '_collector', return_value=self.collector(api)):
            manager._execute('run', cfg)
        result = self.storage.get_run('run')
        self.assertEqual(result['status'], 'partial', result.get('error_message'))
        self.assertEqual(result['stop_reason'], 'source_exhausted')
        self.assertEqual(result['attempts_completed'], 1)
        self.assertEqual(result['collected_by_kind']['test'], 5)
        self.assertEqual(api.create.call_count, 2)
        with patch('medik_pilot.exporter.EXPORT_DIR', Path(self.tmp.name)):
            self.assertEqual(len(json.loads(export_json(self.storage, 'run').read_text(encoding='utf-8'))['tests']), 5)


if __name__ == '__main__':
    unittest.main()
