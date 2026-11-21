import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bossapply.engine.adb_client import AdbClient, AdbTimeoutError, TransientUiDumpError, UiDumpError


class AdbClientTests(unittest.TestCase):
    def setUp(self):
        self.stopped = threading.Event()
        self.adb = AdbClient({'path': sys.executable}, stopped=self.stopped)

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
        with patch.object(self.adb, 'shell') as shell, patch.object(self.adb, 'wait') as wait:
            self.adb.swipe_short()
            shell.assert_called_once_with('input', 'swipe', '500', '1300', '500', '1010', '200')
            shell.reset_mock()
            self.adb.swipe_short(0.5, bounds=(100, 200, 500, 1000))
            shell.assert_called_once_with('input', 'swipe', '300', '720', '300', '320', '200')
            shell.reset_mock()
            with self.assertRaises(ValueError):
                self.adb.swipe_short(0.5, bounds=(100, 200, 500, 200))
            shell.assert_not_called()
            self.assertEqual(wait.call_count, 2)

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
