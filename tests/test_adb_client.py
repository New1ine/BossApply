import os
import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack

from bossapply.engine.adb_client import AdbClient, AdbTimeoutError, TransientUiDumpError, UiDumpError


class AdbClientTests(unittest.TestCase):
    def setUp(self):
        self.stopped = threading.Event()
        self.adb = AdbClient({'path': sys.executable}, stopped=self.stopped)
        selector = patch('bossapply.engine.ui_probe.command_gesture_path', return_value=None, create=True)
        self.gesture_selector = selector.start()
        self.addCleanup(selector.stop)

    def test_read_only_scan_handles_fragmented_device_list(self):
        response = b'ABC123\tdevice usb:2 model:Phone\nXYZ\tunauthorized\n'
        with patch('bossapply.engine.adb_client.socket.create_connection') as connect:
            sock = connect.return_value.__enter__.return_value
            sock.recv.side_effect = [b'O', b'KAY', f'{len(response):04x}'.encode(), response]
            self.adb.port = 5037
            self.assertEqual(self.adb.scan_ports(), [
                {'port': 5037, 'devices': [{'serial': 'ABC123', 'model': 'Phone', 'physical': True}]},
            ])
            sock.sendall.assert_called_once_with(b'000ehost:devices-l')

    def test_scan_distinguishes_unrelated_listener_from_empty_adb_server(self):
        with patch('bossapply.engine.adb_client.socket.create_connection') as connect:
            sock = connect.return_value.__enter__.return_value
            for chunks, expected in [([b'HTTP'], None), ([b'OKAY', b'0000'], [])]:
                with self.subTest(chunks=chunks):
                    sock.recv.side_effect = chunks
                    self.assertEqual(self.adb._read_device_list(5037), expected)

    def test_connection_rejects_ambiguous_selection_without_contacting_phones(self):
        servers = [
            {'port': port, 'devices': [{'serial': f'phone-{port}', 'physical': True}]}
            for port in (5037, 5038)
        ]
        with patch.object(self.adb, 'scan_ports', return_value=servers), \
                patch.object(self.adb, '_run_for_port') as run:
            with self.assertRaisesRegex(RuntimeError, '多个匹配'):
                self.adb.connect()
            run.assert_not_called()

    def test_phone_discovery_and_connection_exclude_emulators(self):
        devices = self.adb._parse_devices(
            b'phone\tdevice usb:2 model:Phone\n'
            b'192.168.1.2:42000\tdevice model:Phone\n'
            b'emulator-5554\tdevice model:sdk_gphone64_x86_64\n'
        )
        servers = [{'port': 5037, 'devices': devices}]
        with patch.object(self.adb, 'scan_ports', return_value=servers), \
                patch.object(self.adb, '_run_for_port', return_value=b'Physical size: 1080x2400\n') as run:
            self.assertEqual(self.adb.discover_devices(), [(5037, devices[0]), (5037, devices[1])])
            self.adb.serial = 'emulator-5554'
            with self.assertRaisesRegex(RuntimeError, '找不到指定手机'):
                self.adb.connect()
            run.assert_not_called()
            self.adb.serial = '192.168.1.2:42000'
            self.assertEqual(self.adb.connect(), '192.168.1.2:42000')
            run.assert_called_once_with(5037, '-s', self.adb.serial, 'shell', 'wm', 'size', timeout=10)

    def test_connection_requires_valid_size_and_uses_the_override(self):
        servers = [{'port': 5037, 'devices': [{'serial': 'one', 'physical': True}]}]
        with patch.object(self.adb, 'scan_ports', return_value=servers), \
                patch.object(self.adb, '_run_for_port') as run:
            run.return_value = b'Unexpected 1080x2400'
            with self.assertRaisesRegex(RuntimeError, '尺寸'):
                self.adb.connect()
            self.assertEqual(self.adb.serial, 'auto')
            run.return_value = b'Physical size: 1080x2400\r\nOverride size: 720x1600\r\n'
            self.assertEqual(self.adb.connect(), 'one')
            self.assertEqual(self.adb.size, (720, 1600))

    def test_manual_pairing_keeps_address_and_writes_code_to_stdin(self):
        address = '192.168.1.10:36789'
        paired = f'Enter pairing code: Successfully paired to {address} [guid=adb-one]'.encode()
        with patch.object(self.adb, '_ensure_server'), \
                patch.object(self.adb, '_run_for_port', return_value=paired) as run:
            self.adb.pair_wifi(address, '012345')
            run.assert_called_once_with(0, 'pair', address, timeout=30, merge_output=True,
                                        input_data=b'012345\n')
        self.assertEqual(self.adb.paired_endpoint, address)
        self.assertEqual(self.adb.paired_guid, 'adb-one')

    def test_manual_connection_keeps_address(self):
        address = '192.168.1.10:36789'
        with patch.object(self.adb, '_ensure_server'), \
                patch.object(self.adb, '_connect_wifi_endpoint') as connect, \
                patch.object(self.adb, 'connect', return_value=address):
            self.assertEqual(self.adb.connect_wifi(address), address)
            connect.assert_called_once_with(address)

    def test_automatic_wifi_connection_requires_paired_host_and_guid(self):
        devices = b'''adb-phone-other _adb-tls-connect._tcp 192.168.1.31:12345
adb-unknown _adb-tls-connect._tcp 192.168.1.30:54321
adb-phone-correct _adb-tls-connect._tcp. 192.168.1.30:34567
'''
        self.assertEqual(self.adb._mdns_connect_endpoint(devices, '192.168.1.30', 'adb-phone'),
                         '192.168.1.30:34567')

    def test_pairing_failure_redacts_code_on_both_adb_output_paths(self):
        with patch.object(self.adb, '_ensure_server'), \
                patch.object(self.adb, '_run_for_port') as run:
            for result in [RuntimeError('rejected 012345'), b'Enter pairing code: rejected 012345']:
                with self.subTest(result=result):
                    run.side_effect = result if isinstance(result, Exception) else None
                    run.return_value = result
                    with self.assertRaisesRegex(RuntimeError, r'rejected \*{6}') as err:
                        self.adb.pair_wifi('192.168.1.10:36789', '012345')
                    self.assertNotIn('012345', str(err.exception))

    def test_cancellation_terminates_running_client_subprocess(self):
        popen = subprocess.Popen
        processes = []
        stop = threading.Timer(0.05, self.stopped.set)
        self.addCleanup(stop.cancel)

        def substitute_adb(_args, **kwargs):
            process = popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
            processes.append(process)
            stop.start()
            return process

        with patch('bossapply.engine.adb_client.subprocess.Popen', side_effect=substitute_adb):
            with self.assertRaises(InterruptedError):
                self.adb._run_for_port(5037, 'devices', timeout=1)
        self.assertIsNotNone(processes[0].poll())

    def test_communicate_preserves_stdin_when_retried_after_poll_timeout(self):
        popen = subprocess.Popen
        code = 'import sys, time; time.sleep(0.35); sys.stdout.buffer.write(sys.stdin.buffer.read())'

        def substitute_adb(_args, **kwargs):
            return popen([sys.executable, '-c', code], **kwargs)

        with patch('bossapply.engine.adb_client.subprocess.Popen', side_effect=substitute_adb):
            self.assertEqual(self.adb._run_for_port(5037, 'pair', input_data=b'012345\n'),
                             b'012345\n')

    def test_command_timeout_terminates_running_client_subprocess(self):
        popen = subprocess.Popen
        processes = []

        def substitute_adb(_args, **kwargs):
            process = popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
            processes.append(process)
            return process

        with patch('bossapply.engine.adb_client.subprocess.Popen', side_effect=substitute_adb):
            with self.assertRaises(AdbTimeoutError):
                self.adb._run_for_port(5037, 'devices', timeout=0.05)
        self.assertIsNotNone(processes[0].poll())

    def test_controlled_command_keeps_stdin_open_then_allows_eof_cleanup_before_stopping(self):
        popen = subprocess.Popen
        for reason in ('stop', 'timeout'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                self.stopped.clear()
                marker = Path(directory) / 'released'
                code = (
                    'import pathlib, sys, time; '
                    'sys.stdin.buffer.read(); '
                    'time.sleep(0.08); '
                    'pathlib.Path(sys.argv[1]).write_text("cancelled"); '
                    'print("released")'
                )
                processes = []
                stop = threading.Timer(0.05, self.stopped.set)
                self.addCleanup(stop.cancel)

                def substitute_adb(_args, **kwargs):
                    process = popen([sys.executable, '-c', code, str(marker)], **kwargs)
                    processes.append(process)
                    if reason == 'stop':
                        stop.start()
                    return process

                with patch('bossapply.engine.adb_client.subprocess.Popen', side_effect=substitute_adb):
                    expected = InterruptedError if reason == 'stop' else AdbTimeoutError
                    with self.assertRaises(expected):
                        self.adb._run_for_port(5037, 'shell', timeout=0.5 if reason == 'stop' else 0.05,
                                               cancel_on_stdin_close=True)
                self.assertEqual(processes[0].poll(), 0)
                self.assertEqual(marker.read_text(), 'cancelled')

    def test_controlled_command_still_terminates_a_client_that_ignores_eof(self):
        popen = subprocess.Popen
        processes = []

        def substitute_adb(_args, **kwargs):
            process = popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
            processes.append(process)
            return process

        with patch('bossapply.engine.adb_client.subprocess.Popen', side_effect=substitute_adb):
            with self.assertRaises(AdbTimeoutError):
                self.adb._run_for_port(5037, 'shell', timeout=0.05, cancel_on_stdin_close=True)
        self.assertIsNotNone(processes[0].poll())

    def test_cleanup_bounds_wait_when_another_process_still_holds_a_pipe(self):
        process = Mock(returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired('adb', 0.2)
        process.wait.side_effect = subprocess.TimeoutExpired('adb', 0.2)
        self.adb._terminate_client(process)
        process.kill.assert_called_once_with()
        process.communicate.assert_called_once_with(timeout=0.2)
        process.wait.assert_called_once_with(timeout=0.2)

    def test_swipe_uses_the_screen_by_default_and_stays_in_supplied_bounds(self):
        self.adb.size = (1000, 2000)
        with patch.object(self.adb, '_command_swipe') as command, patch.object(self.adb, 'wait') as wait:
            self.adb.swipe_short()
            command.assert_called_once_with(500, 1300, 500, 1010, 200)
            command.reset_mock()
            self.adb.swipe_short(0.5, bounds=(100, 200, 500, 1000))
            command.assert_called_once_with(300, 720, 300, 320, 200)
            command.reset_mock()
            with self.assertRaises(ValueError):
                self.adb.swipe_short(0.5, bounds=(100, 200, 500, 200))
            command.assert_not_called()
            wait.assert_not_called()

    def command_swipe_dependencies(self, output=None, *, cached=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        jar = Path(directory) / "gesture's tool.jar"
        jar.write_bytes(b'current dex jar')
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        remote = f'/data/local/tmp/bossapply-scroll-{digest}.jar'
        checksum = f'{digest}  {remote}\n'.encode()
        stack.enter_context(patch.object(self.adb, '_scroll_gesture_path', return_value=jar))
        stack.enter_context(patch('bossapply.engine.adb_client.secrets.token_hex', return_value='a' * 32))
        cleanup_type = stack.enter_context(patch('bossapply.engine.adb_client.AdbClient'))
        run = stack.enter_context(patch.object(self.adb, 'run'))
        metadata = stack.enter_context(patch.object(self.adb, 'shell'))
        metadata.side_effect = [checksum if cached else b'', checksum]
        if output is None:
            output = json.dumps({'protocol': 1, 'profile': 'uniform', 'ok': True}).encode()
        gesture = stack.enter_context(patch.object(self.adb, '_run_for_port', return_value=output))
        return jar, run, gesture, cleanup_type, metadata, remote

    def test_command_swipe_atomically_caches_verified_bytes_then_executes_once(self):
        jar, run, gesture, cleanup_type, metadata, remote = self.command_swipe_dependencies()
        result = self.adb.swipe(100, 1500, 200, 500, duration=1200)
        self.assertTrue(result['ok'])
        temporary = remote + '.' + 'a' * 32 + '.part'
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        run.assert_called_once_with('-s', self.adb.serial, 'push', str(jar), temporary, timeout=10)
        publish = metadata.call_args_list[1].args[0]
        self.assertIn(f'sha256sum {temporary}', publish)
        self.assertIn(f'[ "${{scroll_sha%% *}}" = {digest} ] && mv -f -- {temporary} {remote}', publish)
        self.assertTrue(publish.endswith(f'&& sha256sum {remote}'))
        gesture.assert_called_once_with(
            self.adb.port, '-s', self.adb.serial, 'shell', '-T',
            f'CLASSPATH={remote} app_process / com.bossapply.scrolltest.ScrollGesture '
            '100 1500 200 500 1200 150 --monitor-stdin', timeout=6.2, cancel_on_stdin_close=True,
        )
        cleanup_type.assert_called_once_with({'path': self.adb.path, 'port': self.adb.port,
                                             'serial': self.adb.serial})
        cleanup_type.return_value.shell.assert_not_called()

    def test_command_swipe_reuses_matching_remote_cache_without_push_or_removal(self):
        _, run, gesture, cleanup_type, metadata, remote = self.command_swipe_dependencies(cached=True)
        self.adb.swipe(100, 1500, 100, 500, duration=1200)
        run.assert_not_called()
        cleanup_type.assert_not_called()
        self.assertEqual(metadata.call_count, 1)
        self.assertIn(f'sha256sum {remote}', metadata.call_args.args[0])
        gesture.assert_called_once()

    def test_command_swipe_reuses_the_installed_apk_without_any_jar_deployment(self):
        apk = "/data/app/gesture's package/base.apk"
        self.gesture_selector.return_value = apk
        with patch.object(self.adb, '_cached_scroll_gesture') as deploy, \
                patch.object(self.adb, '_run_for_port', return_value=b'{"protocol":1,"profile":"uniform","ok":true}') as gesture:
            for _ in range(2):
                self.adb.swipe(100, 1500, 100, 500, duration=1200)
            self.gesture_selector.assert_called_once_with(self.adb)
            deploy.assert_not_called()
            self.assertEqual(gesture.call_count, 2)
            self.assertIn(f'CLASSPATH={shlex.quote(apk)} ', gesture.call_args.args[-1])

    def test_command_gesture_selection_is_repeated_when_the_phone_or_server_changes(self):
        self.gesture_selector.side_effect = ['/data/app/probe/base.apk', None, '/data/app/other/base.apk']
        with patch.object(self.adb, '_cached_scroll_gesture', return_value='/data/local/tmp/cached.jar') as deploy, \
                patch.object(self.adb, '_run_for_port', return_value=b'{"protocol":1,"profile":"uniform","ok":true}'):
            self.adb.swipe(100, 1500, 100, 500, duration=1200)
            self.adb.serial = 'another-phone'
            self.adb.swipe(100, 1500, 100, 500, duration=1200)
            self.adb.port = 5038
            self.adb.swipe(100, 1500, 100, 500, duration=1200)
        self.assertEqual(self.gesture_selector.call_count, 3)
        deploy.assert_called_once_with()

    def test_an_unconfirmed_apk_gesture_never_falls_back_to_a_jar_or_repeats(self):
        self.gesture_selector.return_value = '/data/app/probe/base.apk'
        with patch.object(self.adb, '_cached_scroll_gesture') as deploy, \
                patch.object(self.adb, '_run_for_port', side_effect=AdbTimeoutError('unconfirmed')) as gesture:
            with self.assertRaisesRegex(AdbTimeoutError, 'unconfirmed'):
                self.adb.swipe(100, 1500, 100, 500, duration=1200)
            deploy.assert_not_called()
            gesture.assert_called_once()

    def test_changed_local_bytes_select_a_different_cache_even_when_the_old_one_exists(self):
        jar, run, gesture, _, metadata, previous = self.command_swipe_dependencies(cached=True)
        jar.write_bytes(b'updated dex jar')
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        remote = f'/data/local/tmp/bossapply-scroll-{digest}.jar'
        metadata.side_effect = [b'', f'{digest}  {remote}\n'.encode()]
        self.adb.swipe(100, 1500, 100, 500, duration=1200)
        self.assertNotEqual(previous, remote)
        self.assertIn(remote, metadata.call_args_list[0].args[0])
        self.assertTrue(run.call_args.args[4].startswith(remote + '.'))
        self.assertIn(f'CLASSPATH={remote} ', gesture.call_args.args[-1])

    def test_a_corrupt_existing_cache_is_repaired_before_any_gesture(self):
        jar, run, gesture, _, metadata, remote = self.command_swipe_dependencies()
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        metadata.side_effect = [f'{"0" * 64}  {remote}\n'.encode(), f'{digest}  {remote}\n'.encode()]
        self.adb.swipe(100, 1500, 100, 500, duration=1200)
        run.assert_called_once()
        gesture.assert_called_once()

    def test_concurrent_cache_publishers_use_distinct_temporary_files_for_the_same_digest(self):
        jar, run, gesture, cleanup_type, metadata, remote = self.command_swipe_dependencies()
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        probes = threading.Barrier(2)

        def respond(command, **_kwargs):
            if command.startswith('if [ -f '):
                probes.wait(timeout=2)
                return b''
            return f'{digest}  {remote}\n'.encode()

        metadata.side_effect = respond
        results = []
        errors = []

        def publish():
            try:
                results.append(self.adb._cached_scroll_gesture())
            except BaseException as exc:
                errors.append(exc)

        with patch('bossapply.engine.adb_client.secrets.token_hex', side_effect=['a' * 32, 'b' * 32]):
            workers = [threading.Thread(target=publish, daemon=True) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(results, [remote, remote])
        self.assertEqual({call.args[4] for call in run.call_args_list}, {
            remote + '.' + 'a' * 32 + '.part', remote + '.' + 'b' * 32 + '.part',
        })
        gesture.assert_not_called()
        cleanup_type.return_value.shell.assert_not_called()

    def test_command_swipe_cleans_partial_push_and_does_not_issue_input_after_transfer_failure(self):
        _, run, gesture, cleanup_type, _, remote = self.command_swipe_dependencies()
        run.side_effect = RuntimeError('push failed')
        with self.assertRaisesRegex(RuntimeError, 'push failed'):
            self.adb.swipe(100, 1500, 100, 500, duration=1200)
        run.assert_called_once()
        gesture.assert_not_called()
        cleanup_type.return_value.shell.assert_called_once_with(
            'rm', '-f', '--', remote + '.' + 'a' * 32 + '.part', timeout=3,
        )

    def test_a_failed_or_unconfirmed_publish_never_executes_or_removes_the_shared_cache(self):
        for failure in (RuntimeError('checksum mismatch'), b'not a verified checksum'):
            with self.subTest(failure=failure):
                _, _, gesture, cleanup_type, metadata, remote = self.command_swipe_dependencies()
                metadata.side_effect = [b'', failure]
                with self.assertRaises(RuntimeError):
                    self.adb.swipe(100, 1500, 100, 500, duration=1200)
                gesture.assert_not_called()
                cleanup_type.return_value.shell.assert_called_once_with(
                    'rm', '-f', '--', remote + '.' + 'a' * 32 + '.part', timeout=3,
                )

    def test_unconfirmed_command_swipe_is_not_replayed_or_replaced_by_input_swipe(self):
        for failure in (AdbTimeoutError('unconfirmed'), InterruptedError('stopped')):
            with self.subTest(failure=type(failure).__name__):
                _, run, gesture, cleanup_type, _, _ = self.command_swipe_dependencies(cached=True)
                gesture.side_effect = failure
                with self.assertRaises(type(failure)):
                    self.adb.swipe(100, 1500, 100, 500, duration=1200)
                run.assert_not_called()
                gesture.assert_called_once()
                cleanup_type.assert_not_called()

    def test_command_swipe_rejects_invalid_or_old_helper_results_without_retry(self):
        for output in (b'not json', b'{}', b'{"ok":true,"profile":"easeOutCubic"}',
                       b'{"protocol":1,"profile":"uniform","ok":false}'):
            with self.subTest(output=output):
                _, run, gesture, cleanup_type, _, _ = self.command_swipe_dependencies(output, cached=True)
                with self.assertRaisesRegex(RuntimeError, '不会重复发送手势'):
                    self.adb.swipe(100, 1500, 100, 500, duration=1200)
                run.assert_not_called()
                gesture.assert_called_once()
                cleanup_type.assert_not_called()

    def test_temporary_cleanup_failure_preserves_the_deployment_error(self):
        _, run, gesture, cleanup_type, _, _ = self.command_swipe_dependencies()
        run.side_effect = AdbTimeoutError('push uncertain')
        cleanup_type.return_value.shell.side_effect = RuntimeError('device disconnected')
        with self.assertRaisesRegex(AdbTimeoutError, 'push uncertain'):
            self.adb.swipe(100, 1500, 100, 500, duration=1200)
        gesture.assert_not_called()

    def test_source_helper_resolution_rejects_the_legacy_experiment_and_uses_only_the_current_artifact(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            legacy = root / 'dist' / 'verification-tools' / 'ScrollGesture.jar'
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b'legacy')
            with patch('bossapply.engine.adb_client.PROJECT_ROOT', root), \
                    patch.object(sys, 'frozen', False, create=True):
                with self.assertRaisesRegex(RuntimeError, '找不到匀速拖动工具'):
                    self.adb._scroll_gesture_path()
                stale_dist = root / 'dist' / 'BossApplyScrollGesture.jar'
                stale_dist.write_bytes(b'stale dist build')
                with self.assertRaisesRegex(RuntimeError, '找不到匀速拖动工具'):
                    self.adb._scroll_gesture_path()
                current = root / 'build' / 'android' / 'BossApplyScrollGesture.jar'
                current.parent.mkdir(parents=True)
                current.write_bytes(b'current')
                self.assertEqual(self.adb._scroll_gesture_path(), current)

    def test_frozen_helper_resolution_uses_the_bundled_renamed_jar(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            bundled = Path(directory) / 'android-probe' / 'ScrollGesture.jar'
            bundled.parent.mkdir()
            bundled.write_bytes(b'dex jar')
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, '_MEIPASS', directory, create=True):
                self.assertEqual(self.adb._scroll_gesture_path(), bundled)

    def test_missing_uniform_tool_fails_before_contacting_phone(self):
        with patch.object(self.adb, '_scroll_gesture_path', side_effect=RuntimeError('missing tool')), \
                patch.object(self.adb, 'run') as run, patch.object(self.adb, 'shell') as shell:
            with self.assertRaisesRegex(RuntimeError, 'missing tool'):
                self.adb.swipe(100, 1500, 100, 500, duration=1200)
            run.assert_not_called()
            shell.assert_not_called()

    def test_component_swipe_uses_the_existing_session_without_a_shell_or_fixed_wait(self):
        self.adb.size = (1000, 2000)
        self.adb.ui_reader = Mock()
        self.adb.ui_reader.swipe.return_value = {'ok': True}
        with patch.object(self.adb, 'shell') as shell, patch.object(self.adb, 'wait') as wait:
            self.assertEqual(self.adb.swipe(500, 1500, 500, 500, duration=600), {'ok': True})
            self.adb.ui_reader.swipe.assert_called_once_with(500, 1500, 500, 500, 600)
            shell.assert_not_called()
            wait.assert_not_called()

    def test_unconfirmed_component_swipe_never_falls_back_to_another_gesture(self):
        self.adb.ui_reader = Mock()
        self.adb.ui_reader.swipe.side_effect = RuntimeError('unconfirmed gesture')
        with patch.object(self.adb, 'shell') as shell:
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                self.adb.swipe(100, 1500, 100, 500, duration=600)
            self.adb.ui_reader.swipe.assert_called_once()
            shell.assert_not_called()

    def test_swipe_rejects_out_of_screen_coordinates_before_input(self):
        self.adb.size = (1000, 2000)
        with patch.object(self.adb, 'shell') as shell:
            with self.assertRaises(ValueError):
                self.adb.swipe(500, 2000, 500, 500, duration=600)
            shell.assert_not_called()

    def test_input_actions_use_the_requested_settle_delay(self):
        with patch.object(self.adb, 'shell'), patch.object(self.adb, '_command_swipe'), \
                patch.object(self.adb, 'wait') as wait:
            for action, args, delay in (
                (self.adb.tap, (3, 4), 0.0),
                (self.adb.back, (), 0.25),
                (self.adb.swipe_short, (), 0.5),
            ):
                with self.subTest(action=action.__name__):
                    action(*args, settle=delay)
                    wait.assert_called_once_with(delay)
                    wait.reset_mock()

    def test_ui_dump_uses_a_fresh_private_file_and_validates_output(self):
        with patch.object(self.adb, 'shell', return_value=b'<hierarchy><node /></hierarchy>') as shell:
            self.assertEqual(self.adb.dump_ui().tag, 'hierarchy')
            self.adb.dump_ui()
            first = shell.call_args_list[0].args[0]
            self.assertIn('umask 077; tmp=/data/local/tmp/bossapply-', first)
            self.assertIn('rm -f', first)
            self.assertIn(self.adb.UI_DUMP_INTERRUPTED, first)
            self.assertNotEqual(first, shell.call_args_list[1].args[0])
            for invalid in [b'', b'<hierarchy>', b'<html/>', b'<!DOCTYPE hierarchy><hierarchy/>']:
                with self.subTest(invalid=invalid):
                    shell.return_value = invalid
                    with self.assertRaises(UiDumpError):
                        self.adb.dump_ui()

    def test_ui_dump_distinguishes_remote_capture_errors_from_adb_failures(self):
        cases = (
            ('Killed', TransientUiDumpError),
            (f'/system/bin/sh: uiautomator terminated\n{self.adb.UI_DUMP_INTERRUPTED}', TransientUiDumpError),
            ('ERROR: could not get idle state.\nBossApply: no UI dump file was produced', TransientUiDumpError),
            ('ERROR: null root node returned by UiTestAutomationBridge.', TransientUiDumpError),
            ('adb: device offline', UiDumpError),
            ('uiautomator: Permission denied\nBossApply: no UI dump file was produced', UiDumpError),
        )
        for detail, expected in cases:
            with self.subTest(detail=detail):
                cause = RuntimeError('ADB 执行失败：' + detail)
                with patch.object(self.adb, 'shell', side_effect=cause) as shell:
                    with self.assertRaises(expected) as error:
                        self.adb.dump_ui(timeout=8)
                self.assertIs(type(error.exception), expected)
                self.assertIs(error.exception.__cause__, cause)
                self.assertIn(detail, str(error.exception))
                shell.assert_called_once()
                self.assertEqual(shell.call_args.kwargs['timeout'], 8)

    def test_ui_dump_classifies_an_empty_hierarchy_as_transient(self):
        with patch.object(self.adb, 'shell', return_value=b'<hierarchy rotation="0"/>'):
            with self.assertRaisesRegex(TransientUiDumpError, '活动窗口'):
                self.adb.dump_ui()

    def test_modified_bundled_adb_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            for name in AdbClient.WINDOWS_ADB_FILES:
                (source / name).write_text(name, encoding='ascii')
            with patch.dict(os.environ, {'LOCALAPPDATA': str(root)}):
                binary = self.adb._cache_bundled_adb(source)
                binary.write_text('ADB.EXE', encoding='ascii')  # Same length, different bytes.
                with self.assertRaisesRegex(RuntimeError, '不一致'):
                    self.adb._cache_bundled_adb(source)
