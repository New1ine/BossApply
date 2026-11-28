"""Install the optional component and open a task-scoped Android UI reader."""
import json
import re
import secrets
import socket
import struct
import sys
import threading
import time
from pathlib import Path

from .. import PROJECT_ROOT
from .adb_client import AdbClient, ComponentUiTransition, TransientUiDumpError, UiDumpError


PACKAGE = 'com.bossapply.uiprobe'
RUNNER = f'{PACKAGE}/.ProbeInstrumentation'
APK_NAME = 'BossApplyUiProbe.apk'
READER_VERSION = 8
COMMAND_GESTURE_VERSION = 8
GESTURE_ENGINE_VERSION = 1
STARTUP_TIMEOUT = 12


def _apk_path():
    if getattr(sys, 'frozen', False):
        bundled = Path(sys._MEIPASS) / 'android-probe' / APK_NAME
        if bundled.is_file():
            return bundled
        path = Path(sys.executable).resolve().parent / APK_NAME
    else:
        path = PROJECT_ROOT / 'build' / 'android' / APK_NAME
    if not path.is_file():
        raise RuntimeError(f'找不到手机组件安装包。请把 {APK_NAME} 放到 {path.parent}')
    return path


def probe_is_installed(adb):
    output = adb.shell('pm', 'list', 'packages', PACKAGE, timeout=10)
    return f'package:{PACKAGE}'.encode() in output.splitlines()


def _installed_version(adb):
    if not probe_is_installed(adb):
        return None
    output = adb.shell('dumpsys', 'package', PACKAGE, timeout=10)
    version = re.search(rb'^\s*versionCode=(\d+)\b', output, re.MULTILINE)
    return int(version[1]) if version else 0


def component_available(adb):
    return (_installed_version(adb) or 0) >= READER_VERSION


def command_gesture_path(adb):
    """Find the installed shell entry without opening a UiAutomation session.

    uiautomator dump owns UiAutomation in command-reader mode. Loading the
    component APK's DEX with app_process reuses its gesture code while leaving
    that exclusive accessibility connection free for command-based reads.
    """
    if (_installed_version(adb) or 0) < COMMAND_GESTURE_VERSION:
        return None
    output = adb.shell('pm', 'path', PACKAGE, timeout=10)
    paths = []
    for line in output.splitlines():
        if line.startswith(b'package:') and line.endswith(b'/base.apk'):
            path = line[len(b'package:'):]
            # Package-manager metadata goes into a shell command later. Accept
            # only an absolute app-install path with literal path components.
            if (len(path) > 2048 or re.fullmatch(rb'/data/app/[A-Za-z0-9_./=+~-]+/base\.apk', path) is None
                    or any(part in (b'.', b'..', b'') for part in path.split(b'/')[1:])):
                raise RuntimeError('手机组件安装路径无效，请重新安装组件后继续')
            paths.append(path.decode('ascii'))
    if len(paths) != 1:
        raise RuntimeError('未能确认手机组件的安装路径，请重新安装组件后继续')
    return paths[0]


def install_probe(adb):
    apk = _apk_path()
    result = adb.run('-s', adb.serial, 'install', '-r', '-t', str(apk), timeout=60)
    if b'Success' not in result.splitlines() or not component_available(adb):
        raise RuntimeError('未确认手机组件已安装或更新，请检查手机上的安装提示')


def uninstall_probe(adb):
    if not probe_is_installed(adb):
        return
    result = adb.run('-s', adb.serial, 'uninstall', PACKAGE, timeout=25)
    if b'Success' not in result.splitlines() or probe_is_installed(adb):
        raise RuntimeError('未确认手机组件已卸载')


class ComponentReader:
    def __init__(self, adb):
        self.adb = adb
        self._stopped = threading.Event()
        self._finished = threading.Event()
        self._forwarded_port = None
        self._connection = None
        self._runner = None
        self._request_lock = threading.Lock()

    def __enter__(self):
        version = _installed_version(self.adb)
        if version is None:
            raise RuntimeError('所选手机尚未安装组件，请安装组件或选用 ADB 命令')
        if version < READER_VERSION:
            raise RuntimeError('手机上的组件版本过旧，请从电脑端重新点击「安装组件」更新')
        self._transport = {'path': self.adb.path, 'serial': self.adb.serial, 'port': self.adb.port}
        self._socket_name = f'bossapply-ui-{secrets.token_hex(16)}'
        self._token = secrets.token_hex(16)
        try:
            # Use an uncancelled transport to retrieve the allocated port before cleanup.
            transport = AdbClient(self._transport)
            self.adb.check_stop()
            port = transport.run('-s', self.adb.serial, 'forward', 'tcp:0',
                                 f'localabstract:{self._socket_name}', timeout=5).strip()
            if not port.isdigit() or not 0 < int(port) <= 65535:
                raise RuntimeError('ADB 未返回有效的手机组件连接端口')
            self._forwarded_port = int(port)
            self.adb.check_stop()
            runner = threading.Thread(target=self._run_component, daemon=True)
            runner.start()
            self._runner = runner
            self._connection = self._connect_to_component()
            self.adb.ui_reader = self
            return self
        except BaseException:
            self._close()
            raise

    def _run_component(self):
        try:
            runner = AdbClient(self._transport, stopped=self._stopped)
            runner.shell('am', 'instrument', '-w', '-r', '-e', 'socket_name', self._socket_name,
                         '-e', 'token', self._token, '-e', 'protocol', str(READER_VERSION), RUNNER, timeout=None)
        except (OSError, RuntimeError):
            pass  # The waiting desktop task reports the connection failure.
        finally:
            self._finished.set()

    def _connect_to_component(self):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while (remaining := deadline - time.monotonic()) > 0 and not self._finished.is_set():
            self.adb.check_stop()
            connection = None
            try:
                connection = socket.create_connection(('127.0.0.1', self._forwarded_port), min(remaining, 0.5))
                connection.sendall(self._token.encode('ascii') + b'\n')
                if self._receive(connection, 6, min(deadline, time.monotonic() + 1.5)) == b'READY\n':
                    return connection
            except InterruptedError:
                if connection is not None:
                    connection.close()
                raise
            except OSError:
                pass  # ADB forwarding can be ready before the Android socket.
            if connection is not None:
                connection.close()
            self.adb.wait(min(max(0, deadline - time.monotonic()), 0.1))
        self.adb.check_stop()
        raise RuntimeError('手机组件未能启动，请确认已更新组件及手机未锁屏，或选择 ADB 命令后继续')

    def _receive(self, connection, length, deadline):
        received = bytearray()
        while len(received) < length:
            self.adb.check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.settimeout(min(0.2, remaining))
            try:
                chunk = connection.recv(min(length - len(received), 65536))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError
            received.extend(chunk)
        return bytes(received)

    def _exchange(self, request, timeout, max_bytes):
        # Reads and gestures share a framed stream. Never let requests interleave.
        with self._request_lock:
            self.adb.check_stop()
            connection = self._connection
            if connection is None:
                raise ConnectionError('手机组件连接已关闭')
            deadline = time.monotonic() + timeout
            try:
                connection.settimeout(timeout)
                connection.sendall(request)
                status, length = struct.unpack('!BI', self._receive(connection, 5, deadline))
                if status not in (0, 1, 2) or length > max_bytes:
                    raise UiDumpError('手机组件返回了无效的数据')
                return status, self._receive(connection, length, deadline)
            except (OSError, TimeoutError, InterruptedError, UiDumpError):
                # A partial response loses framing; a gesture may already have run.
                connection.close()
                self._connection = None
                raise

    def read(self, timeout, *, wait_for_scroll=False):
        try:
            status, payload = self._exchange(b'S' if wait_for_scroll else b'D', timeout,
                                             self.adb.MAX_UI_DUMP_BYTES)
        except InterruptedError:
            raise
        except TimeoutError as exc:
            raise UiDumpError('手机组件读取超时，请检查手机连接和当前页面后继续') from exc
        except OSError as exc:
            raise UiDumpError('手机组件连接已中断，请检查手机连接或改用 ADB 命令后继续') from exc
        if status == 1:
            if payload in (b'moving', b'changed'):
                raise ComponentUiTransition(payload.decode('ascii'))
            if payload == b'foreign':
                raise TransientUiDumpError('当前活动窗口暂不是 BOSS 直聘')
            if payload == b'unavailable':
                raise TransientUiDumpError('Android 暂未提供活动窗口')
            raise TransientUiDumpError('手机组件暂未返回控件树')
        if status == 2:
            raise UiDumpError('手机组件无法读取当前控件树，请改用 ADB 命令后继续')
        return payload

    def swipe(self, x1, y1, x2, y2, duration, timeout=None):
        """Run one gesture. An uncertain or rejected gesture must never be replayed."""
        values = (x1, y1, x2, y2, duration)
        if any(type(value) is not int for value in values) or not 50 <= duration <= 5000:
            raise ValueError('滑动坐标必须是整数，手势时长须在 50～5000 毫秒内')
        if min(x1, y1, x2, y2) < 0 or max(x1, y1, x2, y2) > 2147483647 or (x1, y1) == (x2, y2):
            raise ValueError('滑动坐标无效')
        request = b'G' + struct.pack('!5i', *values)
        try:
            status, payload = self._exchange(request, timeout or duration / 1000 + 5, 4096)
            if status != 0:
                reason = payload.decode('utf-8', errors='replace')
                raise RuntimeError(f'手机组件未完成滑动（{reason}），请检查当前页面后继续')
            result = json.loads(payload)
            if (not isinstance(result, dict) or result.get('ok') is not True
                    or type(result.get('engine_version')) is not int
                    or result['engine_version'] != GESTURE_ENGINE_VERSION
                    or result.get('motion_profile') != 'uniform'):
                raise ValueError('invalid gesture acknowledgement')
            return result
        except InterruptedError:
            raise
        except (OSError, TimeoutError, ValueError, UiDumpError) as exc:
            raise RuntimeError('手机组件滑动结果未能确认，请检查手机界面后继续；本次动作不会自动重试') from exc

    def _close(self):
        self.adb.ui_reader = None
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        transport = AdbClient(self._transport)
        if self._runner is not None and not self._finished.wait(1):
            self._stopped.set()
            try:
                transport.shell('am', 'force-stop', PACKAGE, timeout=3)
            except (OSError, RuntimeError):
                pass  # Android also closes an idle or disconnected component session.
            self._runner.join(timeout=1)
        if self._forwarded_port is not None:
            try:
                transport.run('-s', self.adb.serial, 'forward', '--remove',
                              f'tcp:{self._forwarded_port}', timeout=3)
            except (OSError, RuntimeError):
                pass  # A disconnected ADB transport removes its forwards.
            self._forwarded_port = None

    def __exit__(self, exc_type, exc_value, traceback):
        self._close()
