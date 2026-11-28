import socket
import struct
import unittest
from unittest.mock import Mock, patch

from bossapply.engine import ui_probe
from bossapply.engine.adb_client import AdbClient, ComponentUiTransition, TransientUiDumpError, UiDumpError


def _frame(status, payload=b''):
    return struct.pack('!BI', status, len(payload)) + payload


class UiProbeTests(unittest.TestCase):
    def test_auto_selection_requires_a_compatible_installed_component(self):
        package_line = f'package:{ui_probe.PACKAGE}'.encode()
        for replies, expected in (
            ([b''], False),
            ([package_line, b'    versionCode=1 minSdk=24'], False),
            ([package_line, b'    versionCode=2 minSdk=24'], False),
            ([package_line, b'    versionCode=3 minSdk=24'], False),
            ([package_line, b'    versionCode=4 minSdk=24'], False),
            ([package_line, b'    versionCode=5 minSdk=24'], False),
            ([package_line, b'    versionCode=6 minSdk=24'], False),
            ([package_line, b'    versionCode=7 minSdk=24'], False),
            ([package_line, b'    versionCode=8 minSdk=24'], True),
        ):
            with self.subTest(replies=replies):
                adb = Mock()
                adb.shell.side_effect = replies
                self.assertEqual(ui_probe.component_available(adb), expected)

    def test_command_gesture_reuses_only_a_compatible_installed_apk(self):
        for version in (None, 7, 8):
            with self.subTest(version=version), patch.object(ui_probe, '_installed_version', return_value=version):
                adb = Mock()
                path = '/data/app/~~token+/com.bossapply.uiprobe-aBc==/base.apk'
                adb.shell.return_value = f'package:{path}\npackage:/data/app/token/split_config.apk\n'.encode()
                self.assertEqual(ui_probe.command_gesture_path(adb), path if version == 8 else None)
                if version == 8:
                    adb.shell.assert_called_once_with('pm', 'path', ui_probe.PACKAGE, timeout=10)
                else:
                    adb.shell.assert_not_called()
                adb.run.assert_not_called()

    def test_command_gesture_rejects_ambiguous_or_shell_active_package_paths(self):
        for output in (b'', b'package:/tmp/base.apk\n',
                       b'package:/data/app/x/../base.apk\n',
                       b'package:/data/app/x$(echo injected)/base.apk\n',
                       b'package:/data/app/x/base.apk\npackage:/data/app/y/base.apk\n'):
            with self.subTest(output=output), patch.object(ui_probe, '_installed_version', return_value=8):
                adb = Mock()
                adb.shell.return_value = output
                with self.assertRaisesRegex(RuntimeError, '安装路径'):
                    ui_probe.command_gesture_path(adb)

    def test_source_apk_is_a_build_asset_instead_of_a_distribution_sidecar(self):
        from pathlib import Path
        import tempfile
        import sys

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            asset = root / 'build' / 'android' / ui_probe.APK_NAME
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b'apk')
            with patch.object(ui_probe, 'PROJECT_ROOT', root), patch.object(sys, 'frozen', False, create=True):
                self.assertEqual(ui_probe._apk_path(), asset)

    def test_uninstall_only_targets_the_exact_probe_package(self):
        adb = Mock(serial='chosen-phone')
        adb.shell.side_effect = [
            f'package:{ui_probe.PACKAGE}.extra'.encode(),
            f'package:{ui_probe.PACKAGE}'.encode(),
            b'',
        ]
        adb.run.return_value = b'Success\r\n'
        ui_probe.uninstall_probe(adb)
        adb.run.assert_not_called()
        ui_probe.uninstall_probe(adb)
        adb.run.assert_called_once_with('-s', 'chosen-phone', 'uninstall', ui_probe.PACKAGE, timeout=25)

    def test_missing_or_old_component_does_not_create_a_forward(self):
        adb = Mock()
        for version, message in ((None, '尚未安装'), (1, '版本过旧')):
            with self.subTest(version=version), patch.object(ui_probe, '_installed_version', return_value=version):
                with self.assertRaisesRegex(RuntimeError, message):
                    with ui_probe.ComponentReader(adb):
                        self.fail('An unsupported component should not start')
        adb.run.assert_not_called()

    def test_reader_continues_after_a_complete_transient_frame(self):
        desktop, phone = socket.socketpair()
        self.addCleanup(desktop.close)
        self.addCleanup(phone.close)
        adb = Mock(MAX_UI_DUMP_BYTES=1024)
        reader = ui_probe.ComponentReader(adb)
        reader._connection = desktop
        xml = b'<hierarchy><node package="com.hpbr.bosszhipin" /></hierarchy>'
        phone.sendall(_frame(1, b'moving') + _frame(0, xml))
        with self.assertRaises(ComponentUiTransition) as transition:
            reader.read(timeout=1, wait_for_scroll=True)
        self.assertEqual(transition.exception.reason, 'moving')
        self.assertEqual(reader.read(timeout=1), xml)
        self.assertEqual(phone.recv(1), b'S')
        self.assertEqual(phone.recv(1), b'D')
        self.assertEqual(AdbClient._parse_ui(xml).find('node').get('package'), 'com.hpbr.bosszhipin')

    def test_window_unavailability_is_distinct_from_expected_transition(self):
        for reason, message in ((b'unavailable', '活动窗口'), (b'foreign', '直聘')):
            with self.subTest(reason=reason):
                desktop, phone = socket.socketpair()
                with desktop, phone:
                    reader = ui_probe.ComponentReader(Mock(MAX_UI_DUMP_BYTES=1024))
                    reader._connection = desktop
                    phone.sendall(_frame(1, reason))
                    with self.assertRaisesRegex(TransientUiDumpError, message) as error:
                        reader.read(timeout=1)
                    self.assertNotIsInstance(error.exception, ComponentUiTransition)

    def test_gesture_uses_one_framed_request_and_preserves_following_read(self):
        desktop, phone = socket.socketpair()
        with desktop, phone:
            reader = ui_probe.ComponentReader(Mock(MAX_UI_DUMP_BYTES=1024))
            reader._connection = desktop
            xml = b'<hierarchy><node package="com.hpbr.bosszhipin" /></hierarchy>'
            phone.sendall(_frame(0, b'{"ok":true,"events":34,"engine_version":1,"motion_profile":"uniform"}')
                          + _frame(0, xml))
            self.assertEqual(reader.swipe(20, 800, 20, 200, 600),
                             {'ok': True, 'events': 34, 'engine_version': 1, 'motion_profile': 'uniform'})
            self.assertEqual(phone.recv(21), b'G' + struct.pack('!5i', 20, 800, 20, 200, 600))
            self.assertEqual(reader.read(timeout=1, wait_for_scroll=True), xml)
            self.assertEqual(phone.recv(1), b'S')

    def test_uniform_gesture_keeps_requested_duration_and_measured_motion_metadata(self):
        desktop, phone = socket.socketpair()
        with desktop, phone:
            reader = ui_probe.ComponentReader(Mock(MAX_UI_DUMP_BYTES=1024))
            reader._connection = desktop
            acknowledgement = (
                b'{"ok":true,"engine_version":1,"motion_profile":"uniform","duration_ms":3000,'
                b'"drag_distance_px":900.0,"drag_speed_px_per_s":300.0,'
                b'"drag_threshold_time_ms":93.333,"terminal_hold_ms":150}'
            )
            phone.sendall(_frame(0, acknowledgement))
            result = reader.swipe(20, 1200, 20, 300, 3000)
            self.assertEqual(phone.recv(21), b'G' + struct.pack('!5i', 20, 1200, 20, 300, 3000))
            self.assertEqual(result['duration_ms'], 3000)
            self.assertEqual(result['motion_profile'], 'uniform')
            self.assertEqual(result['drag_speed_px_per_s'], 300)
            self.assertEqual(result['terminal_hold_ms'], 150)

    def test_gesture_dispatch_timing_is_preserved_without_changing_the_action_packet(self):
        desktop, phone = socket.socketpair()
        with desktop, phone:
            reader = ui_probe.ComponentReader(Mock(MAX_UI_DUMP_BYTES=1024))
            reader._connection = desktop
            acknowledgement = (
                b'{"ok":true,"engine_version":1,"motion_profile":"uniform","duration_ms":3359,'
                b'"timing_scope":"move_dispatch_not_rendered_frames",'
                b'"cadence_target_hz":120.0,"cadence_source":"display_refresh_rate",'
                b'"periodic_move_count":400,"move_interval_count":399,'
                b'"move_interval_mean_ms":8.34,"move_interval_p50_ms":8.31,'
                b'"move_interval_p95_ms":9.1,"move_interval_max_ms":17.0,'
                b'"move_dispatch_hz":119.9,"skipped_move_slots":1,'
                b'"move_injection_mean_ms":0.4,"move_injection_max_ms":1.2}'
            )
            phone.sendall(_frame(0, acknowledgement))
            result = reader.swipe(42, 2425, 42, 623, 3359)
            self.assertEqual(phone.recv(21), b'G' + struct.pack('!5i', 42, 2425, 42, 623, 3359))
            self.assertEqual(result['timing_scope'], 'move_dispatch_not_rendered_frames')
            self.assertEqual(result['periodic_move_count'], 400)
            self.assertEqual(result['move_interval_count'], 399)
            self.assertEqual(result['move_interval_p95_ms'], 9.1)
            self.assertEqual(result['skipped_move_slots'], 1)

    def test_gesture_rejection_is_fatal_without_replay(self):
        for payload in (b'foreground_is_not_boss', b'gesture_injection_unconfirmed',
                        b'gesture_speed_below_safe_drag_threshold'):
            with self.subTest(payload=payload):
                desktop, phone = socket.socketpair()
                with desktop, phone:
                    reader = ui_probe.ComponentReader(Mock(MAX_UI_DUMP_BYTES=1024))
                    reader._connection = desktop
                    phone.sendall(_frame(2, payload))
                    with self.assertRaises(RuntimeError) as error:
                        reader.swipe(20, 800, 20, 200, 600)
                    self.assertNotIsInstance(error.exception, TransientUiDumpError)
                    self.assertEqual(phone.recv(21), b'G' + struct.pack('!5i', 20, 800, 20, 200, 600))
                    phone.settimeout(0.02)
                    with self.assertRaises(socket.timeout):
                        phone.recv(1)

    def test_unknown_or_mismatched_gesture_engine_acknowledgements_stop_without_replay(self):
        for payload in (b'{"ok":true}',
                        b'{"ok":true,"engine_version":2,"motion_profile":"uniform"}',
                        b'{"ok":true,"engine_version":1,"motion_profile":"ease_out"}'):
            with self.subTest(payload=payload):
                desktop, phone = socket.socketpair()
                with desktop, phone:
                    reader = ui_probe.ComponentReader(Mock(MAX_UI_DUMP_BYTES=1024))
                    reader._connection = desktop
                    phone.sendall(_frame(0, payload))
                    with self.assertRaisesRegex(RuntimeError, '不会自动重试'):
                        reader.swipe(20, 800, 20, 200, 600)
                    self.assertEqual(phone.recv(21), b'G' + struct.pack('!5i', 20, 800, 20, 200, 600))
                    phone.settimeout(0.02)
                    with self.assertRaises(socket.timeout):
                        phone.recv(1)

    def test_unconfirmed_gesture_closes_stream_instead_of_replaying(self):
        desktop, phone = socket.socketpair()
        with desktop, phone:
            reader = ui_probe.ComponentReader(Mock(MAX_UI_DUMP_BYTES=1024))
            reader._connection = desktop
            phone.sendall(b'\x00')
            phone.shutdown(socket.SHUT_WR)
            with self.assertRaisesRegex(RuntimeError, '不会自动重试'):
                reader.swipe(20, 800, 20, 200, 600)
            self.assertIsNone(reader._connection)
            self.assertEqual(phone.recv(21), b'G' + struct.pack('!5i', 20, 800, 20, 200, 600))
            self.assertEqual(phone.recv(1), b'')

    def test_invalid_gesture_is_rejected_before_transport(self):
        reader = ui_probe.ComponentReader(Mock(MAX_UI_DUMP_BYTES=1024))
        reader._connection = Mock()
        for arguments in ((20, 800, 20, 800, 600), (20, 800, 20, 200, 5),
                          (-1, 800, 20, 200, 600), (2**40, 800, 20, 200, 600)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                reader.swipe(*arguments)
        reader._connection.sendall.assert_not_called()

    def test_incomplete_or_oversized_frame_stops_the_reader(self):
        for data in (b'\x00', struct.pack('!BI', 0, 1025)):
            with self.subTest(data=data):
                desktop, phone = socket.socketpair()
                with desktop, phone:
                    reader = ui_probe.ComponentReader(Mock(MAX_UI_DUMP_BYTES=1024))
                    reader._connection = desktop
                    phone.sendall(data)
                    phone.shutdown(socket.SHUT_WR)
                    with self.assertRaises(UiDumpError):
                        reader.read(timeout=1)

    def test_context_detaches_and_removes_its_own_forward_after_a_workflow_error(self):
        adb = Mock(serial='chosen-phone', port=5038, path='adb-path')
        connection = Mock()
        with (
            patch.object(ui_probe, '_installed_version', return_value=ui_probe.READER_VERSION),
            patch.object(ui_probe.ComponentReader, '_connect_to_component', return_value=connection),
            patch.object(ui_probe, 'AdbClient') as transport,
        ):
            transport.return_value.run.return_value = b'42424\n'
            with self.assertRaisesRegex(RuntimeError, 'workflow failure'):
                with ui_probe.ComponentReader(adb) as reader:
                    self.assertIs(adb.ui_reader, reader)
                    raise RuntimeError('workflow failure')
        self.assertIsNone(adb.ui_reader)
        connection.close.assert_called_once()
        transport.return_value.run.assert_called_with('-s', 'chosen-phone', 'forward', '--remove',
                                                      'tcp:42424', timeout=3)
