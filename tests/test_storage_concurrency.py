import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from medik_pilot.storage import Storage


class StorageConcurrencyTests(unittest.TestCase):
    def test_reader_does_not_block_commit_and_keeps_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Storage(Path(folder) / 'test.db')
            with store.database() as reader:
                self.assertEqual(reader.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                reader.execute('BEGIN')
                self.assertEqual(reader.execute('SELECT COUNT(*) FROM client_entity_ids').fetchone()[0], 0)
                with ThreadPoolExecutor() as pool:
                    result = pool.submit(store.client_entity_id, 'live', 'Лечебное дело', 'test', 'key')
                    self.assertEqual(result.result(timeout=3), 1)
                self.assertEqual(reader.execute('SELECT COUNT(*) FROM client_entity_ids').fetchone()[0], 0)
            with store.database() as reader:
                self.assertEqual(reader.execute('SELECT COUNT(*) FROM client_entity_ids').fetchone()[0], 1)

    def test_uncommitted_writer_does_not_block_reader_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Storage(Path(folder) / 'test.db')
            with self.assertRaisesRegex(ValueError, 'rollback'):
                with store.database() as writer:
                    writer.execute("INSERT INTO client_entity_ids VALUES (1,'live','Лечебное дело','test','key',1,'now')")
                    with store.database() as reader:
                        reader.execute('PRAGMA busy_timeout=100')
                        self.assertEqual(reader.execute('SELECT COUNT(*) FROM client_entity_ids').fetchone()[0], 0)
                    raise ValueError('rollback')
            self.assertEqual(store.client_entity_id('live', 'Лечебное дело', 'test', 'key'), 1)
