"""GUI-independent task controller and configuration. No browser service."""
import copy
import json
import math
import os
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path

from delivery_stats import DailyDeliveryStats, DeliveryStatsError
from engine.adb_client import AdbClient
from engine.workflow import Workflow

BASE_DIR = (
    Path(os.environ.get("APPDATA") or Path.home()) / "BossApply"
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
CONFIG_PATH = BASE_DIR / "config.json"
DELIVERY_STATS_PATH = BASE_DIR / "delivery_stats.sqlite3"
DEFAULTS = {
    'device': {'path': 'auto', 'port': 0, 'serial': 'auto', 'click_interval': 0.15},
    'run': {'limit': 20, 'timeout': 18, 'scroll_fraction': 0.145, 'max_scroll_attempts': 20},
    'actions': {'image': False, 'quick_phrase': False},
    'image': {'album': 'My Documents', 'index': 1, 'original': True},
    'filters': {'exclude_enabled': False, 'exclude_keywords': [],
                'include_enabled': False, 'include_keywords': []},
}
LOG_LIMIT = 120


def _number(value, convert, field, valid):
    try:
        if isinstance(value, bool) or (convert is int and not isinstance(value, (int, str))):
            raise ValueError
        number = convert(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f'{field}必须是有效数字') from None
    if (convert is float and not math.isfinite(number)) or not valid(number):
        raise ValueError(f'{field}超出允许范围')
    return number


def _boolean(value, field):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('true', 'false'):
            return text == 'true'
    raise ValueError(f'{field}必须是布尔值')


def _text(value, field):
    if not isinstance(value, str) or '\0' in value:
        raise ValueError(f'{field}必须是有效文本')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError(f'{field}包含无效字符') from None
    return value.strip()


def normalize_config(data):
    if not isinstance(data, dict):
        raise ValueError('配置必须是 JSON 对象')
    cfg = copy.deepcopy(DEFAULTS)
    for section, values in data.items():
        if section in cfg:
            if not isinstance(values, dict):
                raise ValueError(f'配置段 {section} 必须是 JSON 对象')
            cfg[section].update(copy.deepcopy(values))
        else:
            cfg[section] = copy.deepcopy(values)

    device = cfg['device']
    run = cfg['run']
    image = cfg['image']
    numeric_fields = (
        (device, 'port', int, 'ADB 端口（0～65535）', lambda n: 0 <= n <= 65535),
        (device, 'click_interval', float, '操作后停顿（0～5 秒）', lambda n: 0 <= n <= 5),
        (run, 'limit', int, '投递上限（1～150）', lambda n: 1 <= n <= 150),
        (run, 'timeout', int, '页面等待上限（3～120 秒）', lambda n: 3 <= n <= 120),
        (run, 'scroll_fraction', float, '短滑屏幕比例（大于 0 且小于 1）', lambda n: 0 < n < 1),
        (run, 'max_scroll_attempts', int, '最大短滑次数（1～100）', lambda n: 1 <= n <= 100),
        (image, 'index', int, '图片序号（至少为 1）', lambda n: n >= 1),
    )
    for section, key, convert, field, valid in numeric_fields:
        section[key] = _number(section[key], convert, field, valid)

    for section, key, field in (
        (device, 'path', 'ADB 路径'), (device, 'serial', 'ADB 设备序列号'),
        (image, 'album', '相册名称'),
    ):
        value = _text(section[key], field)
        if not value:
            raise ValueError(f'{field}必须是非空文本')
        section[key] = value

    cfg['actions']['image'] = _boolean(cfg['actions']['image'], '发送简历图片')
    cfg['actions']['quick_phrase'] = _boolean(cfg['actions']['quick_phrase'], '发送快捷语')
    image['original'] = _boolean(image['original'], '发送原图')
    filters = cfg['filters']
    for kind, field in (('exclude', '反向筛选'), ('include', '正向筛选')):
        enabled_key, keywords_key = f'{kind}_enabled', f'{kind}_keywords'
        filters[enabled_key] = _boolean(filters[enabled_key], field)
        keywords = filters[keywords_key]
        if not isinstance(keywords, list):
            raise ValueError(f'{field}关键词必须是文本列表')
        unique = {}
        for keyword in keywords:
            keyword = _text(keyword, f'{field}关键词')
            if keyword:
                unique.setdefault(keyword.casefold(), keyword)
        filters[keywords_key] = list(unique.values())
    return cfg


def read_config():
    try:
        raw = CONFIG_PATH.read_text(encoding='utf-8-sig')
        return normalize_config(json.loads(raw))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except ValueError as exc:
        raise ValueError(f'{CONFIG_PATH}\n{exc}') from exc


def write_config(cfg):
    content = json.dumps(normalize_config(cfg), ensure_ascii=False, indent=2, allow_nan=False)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=CONFIG_PATH.parent, prefix='.config-', delete=False
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(content + '\n')
        temp_path.replace(CONFIG_PATH)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.state = self._new_state()
        self.delivery_stats = DailyDeliveryStats(DELIVERY_STATS_PATH)

    @staticmethod
    def _new_state(limit=0):
        return dict(running=bool(limit), stopping=False, done=0, limit=limit,
                    logs=deque(maxlen=LOG_LIMIT), log_count=0, device='',
                    result='running' if limit else 'idle')

    def snapshot(self):
        with self.lock:
            state = {**self.state, 'logs': list(self.state['logs'])}
        try:
            state['today_success'] = self.delivery_stats.count_today()
        except DeliveryStatsError:
            state['today_success'] = None
        return state

    def log(self, line):
        with self.lock:
            self.state['logs'].append(line)
            self.state['log_count'] += 1
        if sys.stdout is not None:
            try:
                print(line, flush=True)
            except (OSError, UnicodeError, ValueError):
                pass

    def progress(self, n, title):
        with self.lock:
            count = n - self.state['done']
            if count <= 0:
                return
            self.state['done'] = n
        try:
            self.delivery_stats.add_successes(count)
        except DeliveryStatsError as exc:
            raise DeliveryStatsError(f'本次已确认成功 {n} 份，最近 {count} 份未能写入今日累计。{exc}') from exc

    def _run(self, cfg):
        result = 'complete'
        try:
            self.delivery_stats.prepare()
            adb = AdbClient(cfg['device'], stopped=self.stop_event)
            serial = adb.connect()
            with self.lock:
                self.state['device'] = f'{serial} · {adb.port}'
            self.log('手机已连接：' + serial + ' · 端口 ' + str(adb.port))
            Workflow(adb, cfg, log=self.log, progress=self.progress).run()
        except InterruptedError:
            self.log('已停止')
            result = 'stopped'
        except Exception as exc:
            self.log('已中断：' + str(exc))
            result = 'error'
        finally:
            with self.lock:
                self.state['running'] = False
                self.state['stopping'] = False
                self.state['result'] = result

    def start(self, cfg=None):
        cfg = read_config() if cfg is None else normalize_config(cfg)
        Workflow.validate_config(cfg)
        with self.lock:
            if self.state['running']:
                return False
            previous = self.state
            self.stop_event.clear()
            self.state = self._new_state(cfg['run']['limit'])
            self.thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
            try:
                self.thread.start()
            except RuntimeError:
                self.state = previous
                self.thread = None
                raise
        return True

    def halt(self):
        with self.lock:
            if self.state['running']:
                self.state['stopping'] = True
                self.stop_event.set()


CONTROLLER = Controller()
