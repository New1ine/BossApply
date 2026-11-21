import contextlib
import io
import subprocess
import sys
import tempfile
import textwrap
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Event
from unittest.mock import patch

from bossapply import core, delivery_stats


class ConfigTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent))
        self.path = Path(directory) / 'config.json'
        self.enterContext(patch.object(core, 'CONFIG_PATH', self.path))

    def test_missing_config_defaults_to_no_image(self):
        self.assertFalse(core.read_config()['actions']['image'])
        self.assertFalse(self.path.exists())

    def test_partial_legacy_config_preserves_values_and_normalizes_filters(self):
        self.path.write_text(
            '{"run":{"limit":"7"}, "actions":{"image":true},'
            ' "filters":{"exclude_enabled":"false","include_keywords":[" Python ", "PYTHON", ""]},'
            ' "extra":{"keep":1}}',
            encoding='utf-8-sig',
        )
        cfg = core.read_config()
        self.assertEqual(cfg['run']['limit'], 7)
        self.assertTrue(cfg['actions']['image'])
        self.assertFalse(cfg['filters']['exclude_enabled'])
        self.assertEqual(cfg['filters']['include_keywords'], ['Python'])
        core.write_config(cfg)
        self.assertEqual(core.read_config(), cfg)

    def test_invalid_config_is_rejected_without_modifying_original(self):
        malformed = (
            b'{', b'[]', b'{"run":null}', b'{"run":{"limit":0}}',
            b'{"run":{"limit":1.5}}', b'{"device":{"click_interval":NaN}}',
            b'{"actions":{"image":"yes"}}', b'{"filters":{"exclude_keywords":"Python"}}',
            b'{"filters":{"include_keywords":[null]}}', b'\xff',
        )
        for raw in malformed:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, 'config.json'):
                    core.read_config()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_failed_replace_preserves_existing_file(self):
        core.write_config({'run': {'limit': 7}})
        with patch.object(Path, 'replace', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                core.write_config({'run': {'limit': 8}})
        self.assertEqual(core.read_config()['run']['limit'], 7)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent))
        self.stats_path = Path(directory) / '本地统计 #.sqlite3'
        self.enterContext(patch.object(core, 'DELIVERY_STATS_PATH', self.stats_path))

    def test_empty_include_filter_is_rejected_before_worker_starts(self):
        controller = core.Controller()
        with self.assertRaisesRegex(ValueError, '至少添加一个关键词'):
            controller.start({'filters': {'include_enabled': True}})
        self.assertIsNone(controller.thread)

    def test_counts_new_logs_after_buffer_is_full(self):
        controller = core.Controller()
        with contextlib.redirect_stdout(io.StringIO()):
            for number in range(core.LOG_LIMIT + 3):
                controller.log(str(number))
        state = controller.snapshot()
        self.assertEqual(state['log_count'], core.LOG_LIMIT + 3)
        self.assertEqual(state['logs'], [str(n) for n in range(3, core.LOG_LIMIT + 3)])

    def test_start_uses_passed_snapshot_and_can_be_stopped(self):
        controller = core.Controller()
        entered = Event()
        released = Event()

        class Client:
            def __init__(self, config, stopped):
                self.port = config['port']
                self.serial = config['serial']
                self.stopped = stopped

            def connect(self):
                return self.serial

        class Workflow:
            validate_config = staticmethod(core.Workflow.validate_config)

            def __init__(self, client, config, **callbacks):
                self.client = client

            def run(self):
                entered.set()
                released.wait(2)
                if self.client.stopped.is_set():
                    raise InterruptedError

        with (
            patch.object(core, 'AdbClient', Client),
            patch.object(core, 'Workflow', Workflow),
            patch.object(core, 'read_config', side_effect=AssertionError('unexpected config read')),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                self.assertTrue(controller.start({'device': {'port': 5037, 'serial': 'phone'}}))
                self.assertTrue(entered.wait(2))
                self.assertFalse(controller.start({}))
                self.assertEqual(controller.snapshot()['device'], 'phone · 5037')
                controller.halt()
                self.assertTrue(controller.snapshot()['stopping'])
            finally:
                released.set()
                if controller.thread is not None:
                    controller.thread.join(2)
        self.assertEqual(controller.snapshot()['result'], 'stopped')
        self.assertFalse(controller.snapshot()['running'])

    def test_confirmed_deliveries_persist_through_error_and_another_run(self):
        def first_run(config, *, progress, log):
            progress(1, '职位 A')
            progress(1, '职位 A')
            progress(2, '职位 B')
            raise RuntimeError('后续职位执行失败')

        def second_run(config, *, progress, log):
            progress(1, '职位 C')

        class Workflow:
            validate_config = staticmethod(core.Workflow.validate_config)

            def __init__(self, client, config, **callbacks):
                self.config = config
                self.callbacks = callbacks

            def run(self):
                workflow_runs.pop(0)(self.config, **self.callbacks)

        workflow_runs = [first_run, second_run]
        with (
            patch.object(core, 'AdbClient') as adb,
            patch.object(core, 'Workflow', Workflow),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            adb.return_value.connect.return_value = 'phone'
            first = core.Controller()
            self.assertEqual(first.snapshot()['today_success'], 0)
            self.assertFalse(self.stats_path.exists())
            self.assertTrue(first.start({}))
            first.thread.join(3)
            first_state = first.snapshot()
            self.assertEqual((first_state['result'], first_state['done'], first_state['today_success']),
                             ('error', 2, 2))

            second = core.Controller()
            self.assertEqual(second.snapshot()['today_success'], 2)
            self.assertTrue(second.start({}))
            second.thread.join(3)
            second_state = second.snapshot()
            self.assertEqual((second_state['result'], second_state['done'], second_state['today_success']),
                             ('complete', 1, 3))
            self.assertEqual(core.Controller().snapshot()['today_success'], 3)

    def test_counts_follow_local_date_without_overwriting_earlier_totals(self):
        stats = delivery_stats.DailyDeliveryStats(self.stats_path)
        stats.prepare()
        with patch.object(delivery_stats, 'date') as local_date:
            local_date.today.return_value = date(2026, 12, 31)
            stats.add_successes(3)
            local_date.today.return_value = date(2027, 1, 1)
            self.assertEqual(stats.count_today(), 0)
            stats.add_successes(2)
            self.assertEqual(stats.count_today(), 2)
            local_date.today.return_value = date(2026, 12, 31)
            self.assertEqual(stats.count_today(), 3)

    def test_increment_is_atomic_across_independent_connections(self):
        stats = delivery_stats.DailyDeliveryStats(self.stats_path)
        stats.prepare()
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(stats.add_successes, [1] * 20))
        self.assertEqual(stats.count_today(), 20)

    def test_startup_recovers_previous_totals_after_an_interrupted_write(self):
        stats = delivery_stats.DailyDeliveryStats(self.stats_path)
        stats.prepare()
        stats.add_successes(3)
        interrupted_write = textwrap.dedent("""
            import os
            import sqlite3
            import sys

            db = sqlite3.connect(sys.argv[1])
            db.execute('PRAGMA cache_size=4')
            db.execute('PRAGMA cache_spill=1')
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE daily_deliveries SET success_count=4')
            # Force changes to spill to disk, leaving a rollback journal.
            db.execute('CREATE TABLE unfinished (data BLOB)')
            db.execute('INSERT INTO unfinished VALUES (randomblob(200000))')
            os._exit(0)
        """)
        subprocess.run([sys.executable, '-c', interrupted_write, str(self.stats_path)], check=True, timeout=5)
        self.assertTrue(self.stats_path.with_name(self.stats_path.name + '-journal').exists())
        self.assertEqual(core.Controller().snapshot()['today_success'], 3)

    def test_corrupt_statistics_stop_the_task_before_adb(self):
        self.stats_path.write_bytes(b'Broken database file')
        controller = core.Controller()
        self.assertIsNone(controller.snapshot()['today_success'])
        with patch.object(core, 'AdbClient') as adb, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(controller.start({}))
            controller.thread.join(3)
            adb.assert_not_called()
        state = controller.snapshot()
        self.assertEqual(state['result'], 'error')
        self.assertIn('无法写入投递统计', state['logs'][-1])
        self.assertEqual(self.stats_path.read_bytes(), b'Broken database file')


if __name__ == '__main__':
    unittest.main()
