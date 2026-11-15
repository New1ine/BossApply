"""ADB interaction layer. No GUI dependencies."""
import re
import ipaddress
import secrets
import string
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


class AdbClient:
    DEFAULT_PORTS = tuple(range(5037, 5057))

    def __init__(self, config, stopped=None):
        self.config = config
        self.stopped = stopped
        self.path = self._find_binary(config.get('path', 'auto'))
        self.port = int(config.get('port', 0))  # 0 => detect
        self.serial = str(config.get('serial', 'auto'))
        self.interval = float(config.get('click_interval', 0.15))
        self.size = (1240, 2772)
        self.paired_endpoint = None
        self.paired_guid = None

    @staticmethod
    def _find_binary(value):
        if value and value != 'auto':
            return str(value)

        roots = (
            [Path(sys.executable).resolve().parent, Path(sys._MEIPASS)]
            if getattr(sys, 'frozen', False)
            else [Path(__file__).resolve().parents[1]]
        )
        name = 'adb.exe' if sys.platform == 'win32' else 'adb'

        platform_dir = 'platform-tools-windows' if sys.platform == 'win32' else 'platform-tools-linux'
        for root in roots:
            for candidate in (root / 'platform-tools' / name,
                              root / 'platform-tools' / platform_dir / name):
                if candidate.is_file():
                    return str(candidate)

        return shutil.which(name) or name

    @staticmethod
    def _port_listening(port):
        try:
            with socket.create_connection(('127.0.0.1', int(port)), timeout=0.18):
                return True
        except (OSError, TimeoutError):
            return False

    def _run_for_port(self, port, *args, timeout=5, merge_output=False):
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

        try:
            proc = subprocess.run(
                [self.path, '-P', str(port), *args],
                capture_output=True,
                timeout=timeout,
                check=False,
                creationflags=flags
            )
        except FileNotFoundError as exc:
            raise RuntimeError('找不到 ADB，请安装 Platform-Tools 或检查 ADB 路径') from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'ADB 命令超时（{timeout} 秒）') from exc
        if proc.returncode:
            detail = (proc.stderr or proc.stdout).decode('utf-8', errors='replace').strip()
            raise RuntimeError('ADB 执行失败：' + (detail or str(proc.returncode)))
        return proc.stdout + (b'\n' + proc.stderr if merge_output and proc.stderr else b'')

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

    def scan_ports(self):
        """Return active ADB endpoints. Do not start servers on every possible port."""
        ports = {self.port} if self.port > 0 else set(self.DEFAULT_PORTS)
        result = []
        for port in sorted(ports):
            self.check_stop()
            if not self._port_listening(port):
                continue
            try:
                devices = self._parse_devices(self._run_for_port(port, 'devices', '-l', timeout=3))
            except (RuntimeError, OSError):
                continue  # The listener might be an unrelated service (e.g. adbd).
            result.append({'port': port, 'devices': devices})
        return result

    def _ensure_server(self, servers=None):
        if self.port > 0:
            servers = self.scan_ports() if servers is None else servers
            if any(server['port'] == self.port for server in servers):
                return
            if self._port_listening(self.port):
                raise RuntimeError(f'端口 {self.port} 已被非 ADB 服务占用，请将高级设置中的 ADB 端口改为 0')
            self._run_for_port(self.port, 'start-server', timeout=12)
            return
        servers = self.scan_ports() if servers is None else servers
        if servers:
            self.port = servers[0]['port']
            return
        # Another service may own 5037. Leave it alone and start ADB elsewhere.
        first_free = next((p for p in self.DEFAULT_PORTS if not self._port_listening(p)), None)
        if first_free is None:
            raise RuntimeError('没有可用的 ADB 服务端口，可在高级设置中手动指定')
        self.port = first_free
        self._run_for_port(self.port, 'start-server', timeout=12)

    def discover_devices(self):
        """List connected phones without dumping their UI or selecting one."""
        servers = self.scan_ports()
        if not servers:
            self._ensure_server(servers)
            servers = self.scan_ports()
        return [(server['port'], dev) for server in servers
                for dev in server['devices'] if dev['physical']]

    def connect(self):
        """Find the requested authorized phone, over USB or WiFi."""
        if self.port <= 0:
            servers = self.scan_ports()
            usable = [
                (server['port'], device)
                for server in servers
                for device in server['devices']
                if device['physical']
                and self.serial in ('', 'auto', device['serial'])
            ]
            if usable:
                self.port, device = usable[0]
                if self.serial in ('', 'auto'):
                    self.serial = device['serial']
            else:
                self._ensure_server(servers)

        raw = self._run_for_port(self.port, 'devices', '-l', timeout=10)
        found = self._parse_devices(raw)
        if self.serial in ('', 'auto'):
            physical = [d for d in found if d['physical']]
            if not physical:
                raise RuntimeError('没有找到已授权的手机，请检查 USB 或 WiFi 调试连接')
            self.serial = physical[0]['serial']
        elif self.serial not in [d['serial'] for d in found]:
            raise RuntimeError('找不到指定手机：' + self.serial)
        output = self._run_for_port(self.port, '-s', self.serial,
                                    'shell', 'wm', 'size', timeout=10)
        sizes = re.findall(rb'(\d+)x(\d+)', output)
        if sizes:
            self.size = tuple(map(int, sizes[-1]))
        return self.serial

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
        if not re.fullmatch(r'[0-9]{6}', str(code).strip()):
            raise ValueError('配对码应为手机显示的 6 位数字')
        self._ensure_server()
        endpoint = self._resolve_pair_endpoint(endpoint)
        return self._pair_wifi(endpoint, str(code).strip())

    @classmethod
    def _mdns_endpoint_for_port(cls, raw, service, port):
        matches = set()
        for line in raw.decode('utf-8', errors='replace').splitlines():
            if service not in line:
                continue
            found = re.search(r'((?:\d{1,3}\.){3}\d{1,3}:\d{1,5})\b', line)
            if found:
                try:
                    endpoint = cls._wifi_endpoint(found.group(1))
                except ValueError:
                    continue
                if endpoint.rsplit(':', 1)[1] == str(port):
                    matches.add(endpoint)
        return next(iter(matches)) if len(matches) == 1 else None

    def _resolve_pair_endpoint(self, entered):
        # A VPN can make Android display its tun0 address in the pairing dialog.
        # The same pairing service advertised over mDNS reveals the reachable WLAN IP.
        port = entered.rsplit(':', 1)[1]
        for attempt in range(3):
            try:
                raw = self._run_for_port(self.port, 'mdns', 'services', timeout=5)
                advertised = self._mdns_endpoint_for_port(raw, '_adb-tls-pairing._tcp', port)
                if advertised:
                    return advertised
            except RuntimeError:
                break
            if attempt < 2:
                self.wait(0.3)
        return entered

    def _pair_wifi(self, endpoint, secret):
        try:
            output = self._run_for_port(self.port, 'pair', endpoint, secret,
                                        timeout=30, merge_output=True)
        except RuntimeError as exc:
            detail = str(exc).replace(secret, '******')
            raise RuntimeError('WiFi 配对失败：' + detail +
                               '。ADB 未完成配对连接；请保持手机配对弹窗打开后重试') from exc
        message = output.decode('utf-8', errors='replace').strip()
        if 'successfully paired' not in message.lower():
            raise RuntimeError('WiFi 配对失败：' + (message or '请检查配对地址和配对码'))
        self.paired_endpoint = endpoint
        guid = re.search(r'\[guid=([^\]]+)\]', message)
        self.paired_guid = guid.group(1) if guid else None
        return message

    @staticmethod
    def qr_credentials():
        """Create the AOSP ADB WiFi QR payload; neither secret is persisted."""
        alphabet = string.ascii_letters + string.digits
        name = 'studio-' + ''.join(secrets.choice(alphabet) for _ in range(10))
        password = ''.join(secrets.choice(alphabet) for _ in range(20))
        return name, password, f'WIFI:T:ADB;S:{name};P:{password};;'

    @classmethod
    def _mdns_endpoint(cls, raw, name, service):
        for line in raw.decode('utf-8', errors='replace').splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0].split('._adb', 1)[0] != name or service not in line:
                continue
            match = re.search(r'((?:\d{1,3}\.){3}\d{1,3}:\d{1,5})\b', line)
            if match:
                try:
                    return cls._wifi_endpoint(match.group(1))
                except ValueError:
                    continue
        return None

    @classmethod
    def _mdns_connect_endpoint(cls, raw, host, guid=None):
        for line in raw.decode('utf-8', errors='replace').splitlines():
            if '_adb-tls-connect._tcp' not in line:
                continue
            instance = line.split()[0].split('._adb', 1)[0]
            if guid and not (instance == guid or instance.startswith(guid + '-')):
                continue
            match = re.search(r'((?:\d{1,3}\.){3}\d{1,3}:\d{1,5})\b', line)
            if match:
                try:
                    endpoint = cls._wifi_endpoint(match.group(1))
                except ValueError:
                    continue
                if guid or endpoint.rsplit(':', 1)[0] == host:
                    return endpoint
        return None

    def connect_paired_wifi(self, pair_endpoint, timeout=8):
        """Try the advertised debugging port after pairing; caller can fall back to manual entry."""
        host = self._wifi_endpoint(pair_endpoint).rsplit(':', 1)[0]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self._run_for_port(self.port, 'mdns', 'services', timeout=5)
            endpoint = self._mdns_connect_endpoint(raw, host, self.paired_guid)
            if endpoint:
                return self.connect_wifi(endpoint)
            self.wait(0.8)
        return None

    def pair_wifi_qr(self, name, password, cancelled, timeout=90):
        """Wait for the scanned phone's mDNS pairing service, then pair."""
        self._ensure_server()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled.is_set():
                raise InterruptedError('已取消扫码配对')
            raw = self._run_for_port(self.port, 'mdns', 'services', timeout=5)
            endpoint = self._mdns_endpoint(raw, name, '_adb-tls-pairing._tcp')
            if endpoint:
                self._pair_wifi(endpoint, password)
                return endpoint
            cancelled.wait(1)
        raise RuntimeError('扫码后没有发现手机的配对服务。请检查手机与电脑是否在同一 WiFi，且网络允许局域网设备互访和 mDNS')

    def connect_wifi(self, address):
        """Connect to the phone's debugging port and verify ADB authorization."""
        endpoint = self._wifi_endpoint(address)
        self._ensure_server()
        try:
            raw = self._run_for_port(self.port, 'mdns', 'services', timeout=5)
            advertised = self._mdns_endpoint_for_port(raw, '_adb-tls-connect._tcp',
                                                      endpoint.rsplit(':', 1)[1])
            if advertised:
                endpoint = advertised
        except RuntimeError:
            pass
        output = self._run_for_port(self.port, 'connect', endpoint, timeout=20,
                                    merge_output=True)
        message = output.decode('utf-8', errors='replace').strip().lower()
        if not (message.startswith('connected to ') or message.startswith('already connected to ')):
            raise RuntimeError('WiFi 连接失败：' + (message or '请检查调试地址和端口'))
        devices = self._parse_devices(self._run_for_port(self.port, 'devices', '-l', timeout=10))
        if endpoint not in [device['serial'] for device in devices]:
            raise RuntimeError('WiFi 设备尚未授权，请检查手机上的调试提示')
        self.serial = endpoint
        return self.connect()

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

    def swipe_short(self, fraction=0.145, duration=200):
        width, height = self.size
        x, start = round(width * 0.5), round(height * 0.65)
        end = int(start - height * float(fraction))
        self.shell('input', 'swipe', str(x), str(start), str(x), str(end), str(duration))
        self.wait()

    def dump_ui(self, timeout=30):
        # One ADB round trip; uiautomator itself still performs the expensive dump.
        raw = self.shell(
            'uiautomator dump /sdcard/window.xml >/dev/null && cat /sdcard/window.xml',
            timeout=timeout)
        return ET.fromstring(raw)

