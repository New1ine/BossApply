"""ADB interaction layer. No GUI dependencies."""
import hashlib
import ipaddress
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path


class AdbTimeoutError(RuntimeError):
    """ADB 命令在限定时间内没有完成。"""


class UiDumpError(RuntimeError):
    """ADB 未返回可用的 Android 界面快照。"""


class TransientUiDumpError(UiDumpError):
    """这次抓取没有取得快照，可在原操作的时限内重新抓取。"""


class AdbClient:
    DEFAULT_PORTS = tuple(range(5037, 5057))
    WINDOWS_ADB_FILES = ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll')
    WIFI_PAIRING_SERVICE = '_adb-tls-pairing._tcp'
    WIFI_CONNECT_SERVICE = '_adb-tls-connect._tcp'
    MAX_UI_DUMP_BYTES = 8 * 1024 * 1024
    UI_DUMP_INTERRUPTED = 'BossApply: uiautomator was killed (exit 137)'

    def __init__(self, config, stopped=None):
        self.config = config
        self.stopped = stopped
        self.path = self._find_binary(config.get('path', 'auto'))
        self.port = int(config.get('port', 0))  # 0 => detect
        if not 0 <= self.port <= 65535:
            raise ValueError('ADB 端口必须在 0～65535 之间')
        self.serial = str(config.get('serial', 'auto'))
        self.interval = float(config.get('click_interval', 0.15))
        self.size = (1240, 2772)
        self.paired_endpoint = None
        self.paired_guid = None

    @classmethod
    def _find_binary(cls, value):
        if value and value != 'auto':
            path = Path(value).expanduser()
            if path.is_file():
                return str(path.resolve())
            binary = shutil.which(str(path))
            if binary:
                return str(Path(binary).resolve())
            raise RuntimeError('找不到指定的 ADB 可执行文件，请检查 ADB 路径')

        bundle_root = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else None
        roots = ([Path(sys.executable).resolve().parent, bundle_root] if bundle_root else
                 [Path(__file__).resolve().parents[1]])
        name = 'adb.exe' if sys.platform == 'win32' else 'adb'

        platform_dir = 'platform-tools-windows' if sys.platform == 'win32' else 'platform-tools-linux'
        for root in roots:
            for candidate in (root / 'platform-tools' / name,
                              root / 'platform-tools' / platform_dir / name):
                if candidate.is_file():
                    if root == bundle_root and sys.platform == 'win32':
                        return str(cls._cache_bundled_adb(candidate.parent))
                    return str(candidate)

        # Pass absolute search directories so an unrelated working directory is not searched.
        for directory in filter(os.path.isabs, os.get_exec_path()):
            candidate = Path(directory) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        raise RuntimeError('找不到 ADB，请安装 Platform-Tools 或检查 ADB 路径')

    @classmethod
    def _cache_bundled_adb(cls, source):
        """Keep the Windows ADB server's executable and DLLs outside PyInstaller's temp dir."""
        local_data = Path(os.environ.get('LOCALAPPDATA') or Path.home())
        cache = local_data / 'BossApply' / 'adb'
        digest = hashlib.sha256()
        try:
            files = {name: (source / name).read_bytes() for name in cls.WINDOWS_ADB_FILES}
            for content in files.values():
                digest.update(hashlib.sha256(content).digest())
            target = cache / digest.hexdigest()
            if not target.is_dir():
                cache.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=cache) as temp:
                    staged = Path(temp) / 'platform-tools'
                    staged.mkdir()
                    for name, content in files.items():
                        (staged / name).write_bytes(content)
                    try:
                        staged.rename(target)
                    except FileExistsError:
                        # Another instance has published this version first.
                        pass
            for name, content in files.items():
                cached = target / name
                if cached.stat().st_size != len(content) or cached.read_bytes() != content:
                    raise OSError('缓存目录中的 ADB 文件与内置版本不一致')
        except OSError as exc:
            raise RuntimeError(f'无法准备内置 ADB（{cache}）：{exc}') from exc
        return target / 'adb.exe'

    @staticmethod
    def _port_listening(port):
        try:
            with socket.create_connection(('127.0.0.1', int(port)), timeout=0.18):
                return True
        except (OSError, TimeoutError):
            return False

    def _run_for_port(self, port, *args, timeout=5, merge_output=False, input_data=None):
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        env = None
        if sys.platform.startswith('linux') and getattr(sys, 'frozen', False):
            env = os.environ.copy()
            if 'LD_LIBRARY_PATH_ORIG' in env:
                env['LD_LIBRARY_PATH'] = env['LD_LIBRARY_PATH_ORIG']
            else:
                env.pop('LD_LIBRARY_PATH', None)

        self.check_stop()
        deadline = time.monotonic() + timeout
        try:
            proc = subprocess.Popen(
                [self.path, '-P', str(port), *args],
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags, env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError('找不到 ADB，请安装 Platform-Tools 或检查 ADB 路径') from exc
        try:
            while True:
                self.check_stop()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdbTimeoutError(f'ADB 命令超时（{timeout} 秒）')
                poll = min(0.2, remaining) if self.stopped is not None else remaining
                try:
                    stdout, stderr = proc.communicate(input=input_data, timeout=poll)
                    break
                except subprocess.TimeoutExpired:
                    input_data = None  # communicate() retains any pending input.
            self.check_stop()
        except BaseException:
            self._terminate_client(proc)
            raise
        if proc.returncode:
            detail = (stderr or stdout).decode('utf-8', errors='replace').strip()
            raise RuntimeError('ADB 执行失败：' + (detail or str(proc.returncode)))
        return stdout + (b'\n' + stderr if merge_output and stderr else b'')

    @staticmethod
    def _terminate_client(proc):
        if proc.returncode is not None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.communicate(timeout=0.2)
        except (subprocess.TimeoutExpired, OSError):
            # A server may hold an inherited pipe open. Windows' reader threads own those pipes.
            if sys.platform != 'win32':
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
            try:
                proc.wait(timeout=0.2)
            except (subprocess.TimeoutExpired, OSError):
                pass

    @staticmethod
    def _parse_devices(raw):
        lines = raw.decode('utf-8', errors='replace').splitlines()
        devices = []
        for line in lines:
            parts = line.split()
            if len(parts) < 2 or parts[1] != 'device':
                continue
            serial = parts[0]
            model = next((x[6:] for x in parts[2:] if x.startswith('model:')), '')
            devices.append({'serial': serial, 'model': model,
                            'physical': not serial.startswith('emulator-')})
        return devices

    def _read_device_list(self, port):
        """Probe ADB's read-only host service without triggering CLI server startup or replacement."""
        self.check_stop()
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.18) as sock:
                request = b'host:devices-l'
                sock.sendall(f'{len(request):04x}'.encode('ascii') + request)
                # Bound the whole read even when a non-ADB listener sends bytes slowly.
                deadline = time.monotonic() + 0.25

                def read_exact(size):
                    result = bytearray()
                    while len(result) < size:
                        self.check_stop()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        sock.settimeout(remaining)
                        chunk = sock.recv(size - len(result))
                        if not chunk:
                            raise EOFError
                        result.extend(chunk)
                    return bytes(result)

                if read_exact(4) != b'OKAY':
                    return None
                length = read_exact(4)
                if not re.fullmatch(rb'[0-9a-fA-F]{4}', length):
                    return None
                return self._parse_devices(read_exact(int(length, 16)))
        except InterruptedError:
            raise
        except (OSError, EOFError):
            return None

    def scan_ports(self):
        """Return active local ADB endpoints without starting or restarting a server."""
        ports = (self.port,) if self.port else self.DEFAULT_PORTS
        result = []
        for port in ports:
            devices = self._read_device_list(port)
            if devices is not None:
                result.append({'port': port, 'devices': devices})
        return result

    def _ensure_server(self, servers=None):
        if self.port > 0:
            servers = self.scan_ports() if servers is None else servers
            if any(server['port'] == self.port for server in servers):
                return
            if self._port_listening(self.port):
                raise RuntimeError(f'端口 {self.port} 已被占用且 ADB 无响应，请检查端口设置')
            self._run_for_port(self.port, 'start-server', timeout=12)
            return
        servers = self.scan_ports() if servers is None else servers
        if servers:
            self.port = servers[0]['port']
            return
        # Another service may own 5037. Leave it alone and start ADB elsewhere.
        for port in self.DEFAULT_PORTS:
            self.check_stop()
            if not self._port_listening(port):
                self._run_for_port(port, 'start-server', timeout=12)
                self.port = port
                return
        raise RuntimeError('没有可用的 ADB 服务端口，可在高级设置中手动指定')

    def _discover_servers(self):
        servers = self.scan_ports()
        if not servers:
            self._ensure_server(servers)
            servers = self.scan_ports()
        return servers

    def discover_devices(self):
        """List connected phones without dumping their UI or selecting one."""
        return [(server['port'], dev) for server in self._discover_servers()
                for dev in server['devices'] if dev['physical']]

    def connect(self):
        """Find the requested authorized phone, over USB or WiFi."""
        auto = self.serial in ('', 'auto')
        matches = [(server['port'], device['serial']) for server in self._discover_servers()
                   for device in server['devices']
                   if (device['physical'] if auto else device['serial'] == self.serial)]
        if not matches:
            if auto:
                raise RuntimeError('没有找到已授权的手机，请检查 USB 或 WiFi 调试连接')
            raise RuntimeError('找不到指定手机：' + self.serial)
        if len(matches) > 1:
            raise RuntimeError('发现多个匹配的手机连接，请明确选择手机和 ADB 端口')
        port, serial = matches[0]
        output = self._run_for_port(port, '-s', serial, 'shell', 'wm', 'size', timeout=10)
        sizes = re.findall(rb'^(?:Physical|Override) size: ([1-9][0-9]{0,8})x([1-9][0-9]{0,8})\r?$',
                           output, flags=re.MULTILINE)
        if not sizes:
            raise RuntimeError('无法读取手机屏幕尺寸，请确认手机已连接')
        self.size = tuple(map(int, sizes[-1]))
        self.port, self.serial = port, serial
        return serial

    @staticmethod
    def _wifi_endpoint(value):
        try:
            host, port = value.strip().rsplit(':', 1)
            ipaddress.IPv4Address(host)
            number = int(port)
            if 1 <= number <= 65535:
                return f'{host}:{number}'
        except (AttributeError, ValueError):
            pass
        raise ValueError('请输入手机显示的 IPv4 地址和端口，例如 192.168.1.5:37123')

    def pair_wifi(self, address, code):
        """Pair Android 11+ Wireless debugging using its temporary pairing port."""
        endpoint = self._wifi_endpoint(address)
        code = str(code).strip()
        if not re.fullmatch(r'[0-9]{6}', code):
            raise ValueError('配对码应为手机显示的 6 位数字')
        self._ensure_server()
        return self._pair_wifi(endpoint, code)

    def _pair_wifi(self, endpoint, secret):
        try:
            output = self._run_for_port(self.port, 'pair', endpoint, timeout=30,
                                        merge_output=True, input_data=secret.encode('ascii') + b'\n')
        except RuntimeError as exc:
            detail = str(exc).replace(secret, '******')
            raise RuntimeError('WiFi 配对失败：' + detail +
                               '。ADB 未完成配对连接；请保持手机配对弹窗打开后重试') from None
        message = output.decode('utf-8', errors='replace').strip().removeprefix('Enter pairing code:').strip()
        if not message.lower().startswith('successfully paired to '):
            detail = message.replace(secret, '******') or '请检查配对地址和配对码'
            raise RuntimeError('WiFi 配对失败：' + detail)
        self.paired_endpoint = endpoint
        guid = re.search(r'\[guid=([^\]]+)\]', message)
        self.paired_guid = guid.group(1) if guid else None
        return message.replace(secret, '******')

    @staticmethod
    def qr_credentials():
        """Create the AOSP ADB WiFi QR payload; neither secret is persisted."""
        alphabet = string.ascii_letters + string.digits
        name = 'studio-' + ''.join(secrets.choice(alphabet) for _ in range(10))
        password = ''.join(secrets.choice(alphabet) for _ in range(20))
        return name, password, f'WIFI:T:ADB;S:{name};P:{password};;'

    @classmethod
    def _mdns_services(cls, raw, service):
        for line in raw.decode('utf-8', errors='replace').splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[1].rstrip('.') != service:
                continue
            try:
                yield parts[0].split('._adb', 1)[0], cls._wifi_endpoint(parts[2])
            except ValueError:
                continue

    @classmethod
    def _mdns_endpoint(cls, raw, name, service):
        endpoints = {endpoint for instance, endpoint in cls._mdns_services(raw, service)
                     if instance == name}
        return next(iter(endpoints)) if len(endpoints) == 1 else None

    @classmethod
    def _mdns_connect_endpoint(cls, raw, host, guid=None):
        endpoints = {endpoint for instance, endpoint in cls._mdns_services(raw, cls.WIFI_CONNECT_SERVICE)
                     if endpoint.rsplit(':', 1)[0] == host
                     and (not guid or cls._matches_wifi_guid(instance, guid))}
        return next(iter(endpoints)) if len(endpoints) == 1 else None

    @staticmethod
    def _matches_wifi_guid(instance, guid):
        return instance == guid or instance.startswith(guid + '-')

    def connect_paired_wifi(self, pair_endpoint, timeout=12):
        """Verify the paired phone's wireless transport or connect to its advertised port."""
        host = self._wifi_endpoint(pair_endpoint).rsplit(':', 1)[0]
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            self.check_stop()
            try:
                if self.paired_guid:
                    for device in self._read_device_list(self.port) or []:
                        serial = device['serial']
                        instance, service, _ = serial.partition('.' + self.WIFI_CONNECT_SERVICE)
                        if service and self._matches_wifi_guid(instance, self.paired_guid):
                            self.serial = serial
                            return serial
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                raw = self._run_for_port(self.port, 'mdns', 'services', timeout=min(3, remaining))
                endpoint = self._mdns_connect_endpoint(raw, host, self.paired_guid)
                remaining = deadline - time.monotonic()
                if endpoint and remaining > 0:
                    return self._connect_wifi_endpoint(endpoint, timeout=min(3, remaining))
            except RuntimeError as exc:
                last_error = exc
            self.wait(max(0, min(0.8, deadline - time.monotonic())))
        if last_error:
            raise last_error
        return None

    def pair_wifi_qr(self, name, password, cancelled, timeout=90):
        """Wait for the scanned phone's mDNS pairing service, then pair."""
        if cancelled.is_set():
            raise InterruptedError('已取消扫码配对')
        self._ensure_server()
        deadline = time.monotonic() + timeout
        discovery_timeout = None
        while (remaining := deadline - time.monotonic()) > 0:
            if cancelled.is_set():
                raise InterruptedError('已取消扫码配对')
            try:
                raw = self._run_for_port(self.port, 'mdns', 'services', timeout=min(5, remaining))
            except AdbTimeoutError as exc:
                discovery_timeout = exc
            else:
                discovery_timeout = None
                endpoint = self._mdns_endpoint(raw, name, self.WIFI_PAIRING_SERVICE)
                if endpoint and not cancelled.is_set():
                    self._pair_wifi(endpoint, password)
                    return endpoint
            cancelled.wait(max(0, min(1, deadline - time.monotonic())))
        if cancelled.is_set():
            raise InterruptedError('已取消扫码配对')
        if discovery_timeout:
            raise RuntimeError('ADB 服务发现超时。可切换到「配对码配对」直接配对') from discovery_timeout
        raise RuntimeError('扫码后未发现手机的配对服务。请确认手机与电脑处于可互访的同一局域网，或使用「配对码配对」')

    def connect_wifi(self, address):
        """Connect to the phone's debugging port and verify ADB authorization."""
        endpoint = self._wifi_endpoint(address)
        self._ensure_server()
        self._connect_wifi_endpoint(endpoint)
        return self.connect()

    def _connect_wifi_endpoint(self, endpoint, timeout=20):
        self.check_stop()
        output = self._run_for_port(self.port, 'connect', endpoint, timeout=timeout,
                                    merge_output=True)
        message = output.decode('utf-8', errors='replace').strip().lower()
        if not (message.startswith('connected to ') or message.startswith('already connected to ')):
            raise RuntimeError('WiFi 连接失败：' + (message or '请检查调试地址和端口'))
        devices = self._read_device_list(self.port) or []
        if endpoint not in [device['serial'] for device in devices]:
            raise RuntimeError('WiFi 设备尚未授权，请检查手机上的调试提示')
        self.serial = endpoint
        return self.serial

    def check_stop(self):
        if self.stopped is not None and self.stopped.is_set():
            raise InterruptedError('已停止')

    def run(self, *args, timeout=30):
        self.check_stop()
        return self._run_for_port(self.port, *args, timeout=timeout)

    def shell(self, *args, timeout=30):
        return self.run('-s', self.serial, 'shell', *args, timeout=timeout)

    def wait(self, seconds=None):
        self.check_stop()
        delay = self.interval if seconds is None else float(seconds)
        if self.stopped is not None:
            self.stopped.wait(delay)
        else:
            time.sleep(delay)
        self.check_stop()

    def tap(self, x, y):
        self.shell('input', 'tap', str(int(x)), str(int(y)))
        self.wait()

    def back(self):
        self.shell('input', 'keyevent', '4')
        self.wait()

    def swipe_short(self, fraction=0.145, duration=200, *, bounds=None):
        left, top, right, bottom = (0, 0, *self.size) if bounds is None else bounds
        height = bottom - top
        if left < 0 or top < 0 or left >= right or height < 2:
            raise ValueError('无法在无效的界面区域内滑动')
        x = (left + right) // 2
        start = min(top + round(height * 0.65), bottom - 1)
        end = max(top, int(start - height * float(fraction)))
        self.shell('input', 'swipe', str(x), str(start), str(x), str(end), str(duration))
        self.wait()

    def dump_ui(self, timeout=30):
        # Use one ADB round trip and a fresh file in the shell-only directory.
        command = (
            f'umask 077; tmp=/data/local/tmp/bossapply-{secrets.token_hex(16)}.xml; '
            "trap 'rm -f \"$tmp\"' EXIT; trap 'exit 1' HUP INT TERM; "
            'uiautomator dump "$tmp" >/dev/null; status=$?; '
            f"if [ \"$status\" -eq 137 ]; then printf '%s\\n' '{self.UI_DUMP_INTERRUPTED}' >&2; fi; "
            '[ "$status" -eq 0 ] || exit "$status"; '
            # AOSP may return 0 after printing an error without producing a file.
            "test -s \"$tmp\" || { printf '%s\\n' 'BossApply: no UI dump file was produced' >&2; exit 1; }; "
            f'head -c {self.MAX_UI_DUMP_BYTES + 1} "$tmp"'
        )
        try:
            raw = self.shell(command, timeout=timeout)
        except AdbTimeoutError:
            raise
        except RuntimeError as exc:
            detail = str(exc).removeprefix('ADB 执行失败：')
            messages = {line.strip().casefold() for line in detail.splitlines()}
            if messages.intersection({
                'killed',
                self.UI_DUMP_INTERRUPTED.casefold(),
                'error: could not get idle state.',
                'error: null root node returned by uitestautomationbridge.',
            }):
                raise TransientUiDumpError(f'无法读取手机界面：{exc}') from exc
            raise UiDumpError(f'无法读取手机界面：{exc}') from exc
        if len(raw) > self.MAX_UI_DUMP_BYTES:
            raise UiDumpError('手机界面数据超过允许的大小')
        try:
            document = raw.decode('utf-8-sig')
            if '<!DOCTYPE' in document:
                raise UiDumpError('手机界面数据包含不支持的 XML 声明')
            root = ET.fromstring(document)
        except (UnicodeError, ET.ParseError) as exc:
            raise UiDumpError('手机返回了不完整或无效的界面数据') from exc
        if root.tag != 'hierarchy':
            raise UiDumpError('手机返回了意外的界面数据格式')
        if root.find('node') is None:
            raise TransientUiDumpError('手机当前没有可读取的活动窗口')
        return root

