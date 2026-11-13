"""Explicit, read-only source-history recovery into a caller-owned isolated bank."""
import re

from .collectors.reh2 import Reh2Api, Reh2Error, checked_uuid, parse_report, verify_bank
from .domain import payload_status
from .specialties import PACKAGE_TITLES, normalize_specialty
from .storage import utc_now


class ReadOnlyReh2Api(Reh2Api):
    def request(self, path, body=None, authenticate=True):
        permitted = path in ('/auth/login', '/quiz/bank-grid-data', '/quiz/attempt-grid-data')
        permitted = permitted or bool(re.fullmatch(
            r'/quiz/attempt/[0-9a-fA-F-]{36}(?:/statistics\?mode=FULL)?', path))
        if not permitted:
            raise Reh2Error('Восстановление истории: изменение тренажёра запрещено.')
        return super().request(path, body, authenticate)


def select_history(history, specialty):
    specialty = normalize_specialty(specialty)
    selected, unfinished = [], []
    seen = set()
    for row in history:
        bank = row.get('bank') or {}
        if bank.get('packageName') != PACKAGE_TITLES[specialty]:
            continue
        verify_bank(bank, specialty)
        uid = checked_uuid(row.get('uid'))
        if uid in seen:
            raise Reh2Error('В истории повторяется UUID попытки; импорт остановлен.')
        seen.add(uid)
        (selected if row.get('timeFinished') else unfinished).append(row)
    return sorted(selected, key=lambda r: (r.get('timeFinished', ''), r['uid'])), unfinished


def recover_report(api, storage, row, specialty, account_fingerprint):
    """Only attempt/report reads. Durable raw report precedes idempotent row import."""
    verify_bank(row['bank'], specialty)
    uid = checked_uuid(row['uid'])
    if not row.get('timeFinished'):
        raise Reh2Error('Незавершённая попытка не импортируется и не завершается автоматически.')
    run_id = 'history-' + account_fingerprint[:16] + '-' + uid
    if not storage.get_run(run_id):
        storage.create_run(run_id, dict(source_mode='live', test_source='reh2',
            specialty=specialty, material_type='test', document_mode='new',
            document_name='История REH2 ' + specialty, reference_tests=0,
            reference_cases=0, verification_percent=0, max_attempts=1,
            max_requests=100000, max_duration_minutes=30, delay_seconds=0,
            allow_create_attempts=False, allow_answer_submission=False))
    cached = storage.reh2_report(run_id, 1)
    expected = None
    if cached is None:
        attempt = api.attempt(uid)
        if not isinstance(attempt, dict) or attempt.get('uid') != uid or not attempt.get('timeFinished'):
            raise Reh2Error('Отчёт истории: попытка изменилась или не завершена.')
        verify_bank(attempt.get('bank'), specialty)
        if attempt['bank']['uid'] != row['bank']['uid']:
            raise Reh2Error('Отчёт истории: пакет попытки изменился.')
        questions = attempt.get('questions')
        if isinstance(questions, list) and questions:
            expected = len(questions)
        cached = api.report(uid)
        # Validate before persistence, but save the entire raw response before imports.
        items = parse_report(cached, specialty, row['bank']['packageId'], uid, 1, expected)
        storage.save_reh2_report(run_id, 1, cached)
    else:
        items = parse_report(cached, specialty, row['bank']['packageId'], uid, 1)
    outcomes = {}
    for item in items:
        outcome, _ = storage.store_item(run_id, 'live', specialty, item, 1)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    ready = sum(payload_status('test', item.payload) == 'ready' for item in items)
    storage.update_run(run_id, status='completed' if ready == len(items) else 'partial',
        stop_reason='history_report_imported', attempts_completed=1, finished_at=utc_now())
    return dict(attempt_uid=uid, package=row['bank']['packageId'], run_id=run_id,
                received=len(items), ready=ready, invalid=len(items)-ready, outcomes=outcomes)
