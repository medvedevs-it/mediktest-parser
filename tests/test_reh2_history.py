import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_reh2 import FakeApi, GENERAL, PEDIATRICS, bank
from medik_pilot.reh2_history import ReadOnlyReh2Api, select_history, recover_report
from medik_pilot.collectors.reh2 import Reh2Error
from medik_pilot.storage import Storage


class HistoryTests(unittest.TestCase):
    def test_source_mutations_rejected_before_network(self):
        api = ReadOnlyReh2Api('u', 'p')
        with patch('medik_pilot.collectors.reh2.urllib.request.urlopen') as network:
            for path in ('/quiz/start-attempt', '/quiz/finish-attempt/a', '/quiz/reset-history'):
                with self.assertRaises(Reh2Error):
                    api.request(path)
            network.assert_not_called()

    def test_history_selection_specialty_and_unfinished(self):
        a, b = FakeApi(PEDIATRICS), FakeApi(GENERAL)
        uid = a.create(a.bank)
        a.finish(uid)
        a.create(a.bank)
        b.create(b.bank)
        selected, unfinished = select_history(a.history() + b.history(), PEDIATRICS)
        self.assertEqual([r['uid'] for r in selected], [uid])
        self.assertEqual(len(unfinished), 1)

    def test_replay_reuses_report_and_has_no_external_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Storage(Path(directory) / 'audit.db')
            api = FakeApi(PEDIATRICS)
            uid = api.create(api.bank)
            api.finish(uid)
            row = api.history()[0]
            api.create = Mock(side_effect=AssertionError('create forbidden'))
            api.finish = Mock(side_effect=AssertionError('finish forbidden'))
            fingerprint = hashlib.sha256(b'account').hexdigest()
            result = recover_report(api, store, row, PEDIATRICS, fingerprint)
            self.assertEqual(result['ready'], 5)
            api.report = Mock(side_effect=AssertionError('cached report expected'))
            repeated = recover_report(api, store, row, PEDIATRICS, fingerprint)
            self.assertEqual(repeated['outcomes'], {'replayed': 5})
            self.assertEqual(store.reh2_run_totals(result['run_id'])['seen'], 5)
            api.create.assert_not_called()
            api.finish.assert_not_called()

    def test_reject_other_specialty_or_active_attempt(self):
        api = FakeApi(GENERAL)
        api.create(api.bank)
        with self.assertRaises(Reh2Error):
            recover_report(api, None, api.history()[0], PEDIATRICS, 'abc')
        with self.assertRaises(Reh2Error):
            recover_report(api, None, api.history()[0], GENERAL, 'abc')

    def test_partial_import_resumes_without_duplicate_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Storage(Path(directory) / 'audit.db')
            api = FakeApi(PEDIATRICS)
            api.finish(api.create(api.bank))
            row = api.history()[0]
            original = store.store_item
            calls = 0
            def interrupted(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise RuntimeError('simulated interrupted write')
                return original(*args, **kwargs)
            with patch.object(store, 'store_item', side_effect=interrupted):
                with self.assertRaises(RuntimeError):
                    recover_report(api, store, row, PEDIATRICS, 'account')
            api.report = Mock(side_effect=AssertionError('cached report expected'))
            result = recover_report(api, store, row, PEDIATRICS, 'account')
            self.assertEqual(result['outcomes'], {'replayed': 2, 'new': 3})
            self.assertEqual(store.reh2_run_totals(result['run_id'])['seen'], 5)
