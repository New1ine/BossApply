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

    def test_missing_config_uses_defaults_without_creating_a_file(self):
        cfg = core.read_config()
        self.assertNotIn('target', cfg['device'])
        self.assertFalse(cfg['actions']['image'])
        self.assertEqual(cfg['image']['indices'], [1])
        self.assertEqual(cfg['run']['limit'], 100)
        self.assertEqual(cfg['filters']['min_daily_salary'], 100.0)
        self.assertEqual(cfg['filters']['min_monthly_salary_k'], 3.0)
        self.assertIsNone(cfg['filters']['max_daily_salary'])
        self.assertIsNone(cfg['filters']['max_monthly_salary_k'])
        self.assertFalse(self.path.exists())

    def test_partial_legacy_config_preserves_values_and_normalizes_filters(self):
        self.path.write_text(
            '{"run":{"limit":"7"}, "actions":{"image":true}, "image":{"index":"3"},'
            ' "filters":{"exclude_enabled":"false","include_keywords":[" Python ", "PYTHON", ""]},'
            ' "extra":{"keep":1}}',
            encoding='utf-8-sig',
        )
        cfg = core.read_config()
        self.assertEqual(cfg['run']['limit'], 7)
        self.assertTrue(cfg['actions']['image'])
        self.assertEqual(cfg['image']['indices'], [3])
        self.assertNotIn('index', cfg['image'])
        self.assertFalse(cfg['filters']['exclude_enabled'])
        self.assertEqual(cfg['filters']['include_keywords'], ['Python'])
        self.assertEqual(cfg['filters']['require_include_keywords'], [])
        self.assertEqual(cfg['filters']['min_daily_salary'], 100.0)
        self.assertEqual(cfg['filters']['min_monthly_salary_k'], 3.0)
        self.assertIsNone(cfg['filters']['max_daily_salary'])
        self.assertIsNone(cfg['filters']['max_monthly_salary_k'])
        self.assertTrue(cfg['filters']['allow_negotiable_salary'])
        core.write_config(cfg)
        self.assertEqual(core.read_config(), cfg)

    def test_legacy_device_settings_migrate_to_phone_only(self):
        for serial in ('auto', 'ABC123', '192.168.1.5:37123'):
            with self.subTest(serial=serial):
                core.write_config({'device': {'target': 'phone', 'port': 5038, 'serial': serial}})
                self.assertEqual(core.read_config()['device'], {
                    'path': 'auto', 'port': 5038, 'serial': serial, 'click_interval': 0, 'ui_reader': 'command',
                })
        for legacy in ({'target': 'emulator'}, {'serial': 'emulator-5554'}):
            with self.subTest(legacy=legacy):
                core.write_config({'device': {
                    **legacy, 'path': 'local-sdk/adb.exe', 'port': 5047, 'click_interval': 0.08,
                }})
                self.assertEqual(core.read_config()['device'], {
                    'path': 'auto', 'port': 0, 'serial': 'auto', 'click_interval': 0, 'ui_reader': 'command',
                })

    def test_legacy_timing_settings_cannot_override_internal_policy(self):
        core.write_config({'timing': {
            'ui_dump_retry_delay': '0', 'ui_dump_timeout': '9.5',
            'poll_interval': None, 'job_swipe_duration': '250', 'album_swipe_duration': '450',
        }})
        self.assertEqual(core.read_config()['timing'], core.DEFAULTS['timing'])

    def test_invalid_legacy_runtime_tuning_does_not_block_business_settings(self):
        for timing in (None, [], 'obsolete', {'ui_dump_timeout': 0, 'poll_interval': 'NaN'}):
            with self.subTest(timing=timing):
                core.write_config({
                    'device': {'click_interval': float('nan')},
                    'run': {'limit': 7, 'timeout': 'invalid', 'scroll_fraction': None,
                            'max_scroll_attempts': -1},
                    'timing': timing,
                    'filters': {'require_include_keywords': ['RTOS']},
                })
                cfg = core.read_config()
                self.assertEqual(cfg['device']['click_interval'], 0)
                self.assertEqual(cfg['run'], {**core.DEFAULTS['run'], 'limit': 7})
                self.assertEqual(cfg['timing'], core.DEFAULTS['timing'])
                self.assertEqual(cfg['filters']['require_include_keywords'], ['RTOS'])

    def test_delivery_target_clamps_whole_numbers_and_saves_the_result(self):
        cases = (
            (-10, 1), ('0', 1), ('001', 1), ('37', 37), (150, 150), ('9999', 150),
            ('０５', 1), (' 37 ', 1), ('+37', 1), ('1_00', 1), ('1.5', 1), (1.5, 1), ('', 1), (True, 1),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(core.normalize_delivery_target(value), expected)
                core.write_config({'run': {'limit': value}})
                self.assertEqual(core.read_config()['run']['limit'], expected)
        self.assertEqual(core.normalize_delivery_target('9' * 5000), 150)

    def test_image_indices_keep_tag_order_and_round_trip(self):
        core.write_config({'actions': {'image': True}, 'image': {'indices': ['3', 1, 3], 'index': 2}})
        self.assertEqual(core.read_config()['image'], {
            'album': 'My Documents', 'indices': [3, 1], 'original': True,
        })

    def test_removed_navigation_and_step_settings_are_discarded_from_old_configs(self):
        self.path.write_text('''{
            "actions": {"image": true},
            "device": {"click_interval": 0.5},
            "image": {"indices": [3], "reuse_album_navigation": true},
            "timing": {"screenshot_timeout": 2, "screenshot_interval": 0.1,
                       "screenshot_attempts": 3, "foreground_timeout": 1,
                       "chat_timeout": 7, "chat_settle": 0.08, "poll_interval": 0.2}
        }''', encoding='utf-8')
        cfg = core.read_config()
        self.assertTrue(cfg['actions']['image'])
        self.assertEqual(cfg['device']['click_interval'], 0)
        self.assertEqual(cfg['image']['indices'], [3])
        self.assertNotIn('reuse_album_navigation', cfg['image'])
        self.assertEqual(set(cfg['timing']), set(core.DEFAULTS['timing']))
        self.assertEqual(cfg['timing']['poll_interval'], core.DEFAULTS['timing']['poll_interval'])
        core.write_config(cfg)
        content = self.path.read_text(encoding='utf-8')
        self.assertNotIn('screenshot', content)
        self.assertNotIn('chat_timeout', content)
        self.assertNotIn('chat_settle', content)

    def test_empty_photo_selection_defaults_to_first_photo(self):
        self.path.write_text('{"actions":{"image":true},"image":{"indices":[]}}', encoding='utf-8')
        cfg = core.read_config()
        self.assertTrue(cfg['actions']['image'])
        self.assertEqual(cfg['image']['indices'], [1])
        core.write_config(cfg)
        self.assertEqual(core.read_config()['image']['indices'], [1])

    def test_saves_separate_keyword_scopes_and_optional_salary_thresholds(self):
        core.write_config({'filters': {
            'exclude_enabled': True, 'exclude_keywords': ['C++'],
            'require_include_enabled': True, 'require_include_keywords': [' STM32 ', 'stm32'],
            'min_daily_salary': ' ', 'min_monthly_salary_k': ' 15.5 ',
            'max_daily_salary': ' 300 ', 'max_monthly_salary_k': ' 15.5 ', 'allow_negotiable_salary': 'false',
        }})
        filters = core.read_config()['filters']
        self.assertTrue(filters['exclude_enabled'])
        self.assertEqual(filters['exclude_keywords'], ['C++'])
        self.assertTrue(filters['require_include_enabled'])
        self.assertEqual(filters['require_include_keywords'], ['STM32'])
        self.assertIsNone(filters['min_daily_salary'])
        self.assertEqual(filters['min_monthly_salary_k'], 15.5)
        self.assertEqual(filters['max_daily_salary'], 300.0)
        self.assertEqual(filters['max_monthly_salary_k'], 15.5)
        self.assertFalse(filters['allow_negotiable_salary'])

        filters['max_daily_salary'] = ''
        filters['max_monthly_salary_k'] = ' '
        core.write_config({'filters': filters})
        filters = core.read_config()['filters']
        self.assertIsNone(filters['max_daily_salary'])
        self.assertIsNone(filters['max_monthly_salary_k'])

    def test_legacy_null_salary_thresholds_remain_disabled(self):
        self.path.write_text(
            '{"filters":{"min_daily_salary":null,"min_monthly_salary_k":null}}', encoding='utf-8',
        )
        cfg = core.read_config()
        self.assertIsNone(cfg['filters']['min_daily_salary'])
        self.assertIsNone(cfg['filters']['min_monthly_salary_k'])

    def test_invalid_config_is_rejected_without_modifying_original(self):
        malformed = (
            b'{', b'[]', b'{"run":null}',
            b'{"device":{"ui_reader":"unknown"}}',
            b'{"actions":{"image":"yes"}}', b'{"filters":{"exclude_keywords":"Python"}}',
            b'{"filters":{"include_keywords":[null]}}', b'\xff',
            b'{"image":{"index":null}}', b'{"image":{"indices":2}}',
            b'{"image":{"indices":[true]}}', b'{"image":{"indices":[0]}}',
            b'{"image":{"indices":[null]}}',
            b'{"filters":{"require_exclude_keywords":"C++"}}',
            b'{"filters":{"min_daily_salary":0}}', b'{"filters":{"min_monthly_salary_k":"NaN"}}',
            b'{"filters":{"max_daily_salary":0}}', b'{"filters":{"max_monthly_salary_k":"NaN"}}',
            b'{"filters":{"min_daily_salary":200,"max_daily_salary":199}}',
            b'{"filters":{"min_monthly_salary_k":20,"max_monthly_salary_k":19}}',
            b'{"filters":{"allow_negotiable_salary":"yes"}}',
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

    def test_resume_switches_reader_without_changing_runtime_policy_or_business_settings(self):
        controller = core.Controller()
        task = core._DeliveryTask(core.normalize_config({
            'device': {'serial': 'phone'}, 'run': {'limit': 5}, 'image': {'indices': [3]},
        }), device=('phone', 5037))
        task.checkpoint.success = 2
        controller.task = task
        controller.state['can_resume'] = True
        with patch.object(controller, '_start_worker', return_value=True) as start_worker:
            self.assertTrue(controller.resume({
                'device': {'serial': 'different-phone', 'click_interval': '0.07', 'ui_reader': 'component'},
                'run': {'limit': 50, 'scroll_fraction': '0.2'},
                'image': {'indices': [1]},
                'timing': {'poll_interval': '0.25'},
            }))
        resumed = start_worker.call_args.args[0]
        self.assertEqual(start_worker.call_args.kwargs, {'resuming': True})
        self.assertIs(resumed.checkpoint, task.checkpoint)
        self.assertEqual(resumed.device, task.device)
        self.assertEqual(resumed.config['device']['serial'], 'phone')
        self.assertEqual(resumed.config['device']['click_interval'], 0)
        self.assertEqual(resumed.config['device']['ui_reader'], 'component')
        self.assertEqual(resumed.config['run']['limit'], 5)
        self.assertEqual(resumed.config['run']['scroll_fraction'], core.DEFAULTS['run']['scroll_fraction'])
        self.assertEqual(resumed.config['image']['indices'], [3])
        self.assertEqual(resumed.config['timing'], core.DEFAULTS['timing'])
        self.assertEqual(task.config['timing']['poll_interval'], core.DEFAULTS['timing']['poll_interval'])

    def test_resume_does_not_use_a_reader_detected_on_another_phone(self):
        controller = core.Controller()
        controller.task = core._DeliveryTask(core.normalize_config({}), device=('original-phone', 5037))
        controller.state['can_resume'] = True
        with patch.object(controller, '_start_worker', return_value=True) as start_worker:
            self.assertTrue(controller.resume({'device': {'ui_reader': 'component', 'click_interval': 0.5}},
                                              reader_device=('another-phone', 5037)))
        options = start_worker.call_args.args[0].config['device']
        self.assertEqual(options['ui_reader'], 'command')
        self.assertEqual(options['click_interval'], 0)

    def test_confirmed_deliveries_persist_through_error_and_another_run(self):
        def first_run(config, *, progress, **_callbacks):
            progress(1, '职位 A')
            progress(1, '职位 A')
            progress(2, '职位 B')
            raise RuntimeError('后续职位执行失败')

        def second_run(config, *, progress, **_callbacks):
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
