"""GUI-independent task controller and configuration. No browser service."""
import copy
import json
import math
import os
import sys
import tempfile
import threading
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import PROJECT_ROOT
from .delivery_stats import DailyDeliveryStats, DeliveryStatsError
from .engine.adb_client import AdbClient
from .engine.ui_probe import ComponentReader
from .engine.workflow import TIMING_DEFAULTS, DeliveryCheckpoint, Workflow

BASE_DIR = (
    Path(os.environ.get("APPDATA") or Path.home()) / "BossApply"
    if getattr(sys, "frozen", False)
    else PROJECT_ROOT
)
CONFIG_PATH = BASE_DIR / "config.json"
DELIVERY_STATS_PATH = BASE_DIR / "delivery_stats.sqlite3"
DEFAULTS = {
    'device': {'path': 'auto', 'port': 0, 'serial': 'auto', 'click_interval': 0, 'ui_reader': 'command'},
    'run': {'limit': 100, 'timeout': 18, 'scroll_fraction': 0.145, 'max_scroll_attempts': 20},
    'timing': TIMING_DEFAULTS.copy(),
    'actions': {'image': False, 'quick_phrase': False},
    'image': {'album': 'My Documents', 'indices': [1], 'original': True},
    'filters': {
        'exclude_enabled': False, 'exclude_keywords': [],
        'include_enabled': False, 'include_keywords': [],
        'require_exclude_enabled': False, 'require_exclude_keywords': [],
        'require_include_enabled': False, 'require_include_keywords': [],
        'min_daily_salary': 100.0, 'min_monthly_salary_k': 3.0,
        'max_daily_salary': None, 'max_monthly_salary_k': None,
        'allow_negotiable_salary': True,
    },
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


def normalize_delivery_target(value):
    if isinstance(value, str):
        if not (value.isascii() and value.isdecimal()):
            return 1
        digits = value.lstrip('0')
        value = 150 if len(digits) > 3 else int(digits or '0')
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return max(1, min(150, value))


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
        if section == 'timing':
            # Earlier versions persisted tuning values, including obsolete or
            # invalid ones. Timing is now an internal policy, not user input.
            continue
        if section in cfg:
            if not isinstance(values, dict):
                raise ValueError(f'配置段 {section} 必须是 JSON 对象')
            cfg[section].update(copy.deepcopy(values))
        else:
            cfg[section] = copy.deepcopy(values)

    device = cfg['device']
    old_target = device.pop('target', None)
    old_serial = device['serial']
    if old_target == 'emulator' or (isinstance(old_serial, str) and old_serial.strip().startswith('emulator-')):
        device.update(path='auto', port=0, serial='auto')
    if device['ui_reader'] not in ('command', 'component'):
        raise ValueError('控件读取方式必须是 ADB 命令或手机组件')
    run = cfg['run']
    image = cfg['image']
    run['limit'] = normalize_delivery_target(run['limit'])
    device['click_interval'] = 0
    for key in ('timeout', 'scroll_fraction', 'max_scroll_attempts'):
        run[key] = DEFAULTS['run'][key]
    numeric_fields = (
        (device, 'port', int, 'ADB 端口（0～65535）', lambda n: 0 <= n <= 65535),
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

    cfg['actions']['image'] = _boolean(cfg['actions']['image'], '发送图片')
    cfg['actions']['quick_phrase'] = _boolean(cfg['actions']['quick_phrase'], '发送快捷语')
    image['original'] = _boolean(image['original'], '发送原图')
    image.pop('reuse_album_navigation', None)
    if 'index' in image:
        legacy_index = image.pop('index')
        if 'indices' not in data['image']:
            image['indices'] = [legacy_index]
    indices = image['indices']
    if not isinstance(indices, list):
        raise ValueError('图片序号必须是列表')
    image['indices'] = list(dict.fromkeys(
        _number(index, int, '图片序号（至少为 1）', lambda n: n >= 1)
        for index in (indices or DEFAULTS['image']['indices'])
    ))

    filters = cfg['filters']
    filters['allow_negotiable_salary'] = _boolean(filters['allow_negotiable_salary'], '允许面议')
    for kind, field in (
        ('exclude', '岗位名称反向筛选'), ('include', '岗位名称正向筛选'),
        ('require_exclude', '岗位要求反向筛选'), ('require_include', '岗位要求正向筛选'),
    ):
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
    for key, field in (
        ('min_daily_salary', '最低日薪'), ('min_monthly_salary_k', '最低月薪'),
        ('max_daily_salary', '最高日薪'), ('max_monthly_salary_k', '最高月薪'),
    ):
        value = filters[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            filters[key] = None
        else:
            filters[key] = _number(value, float, field, lambda n: n > 0)
    for minimum_key, maximum_key, label in (
        ('min_daily_salary', 'max_daily_salary', '日薪'),
        ('min_monthly_salary_k', 'max_monthly_salary_k', '月薪'),
    ):
        minimum, maximum = filters[minimum_key], filters[maximum_key]
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f'最高{label}不能低于最低{label}')
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


@dataclass
class _DeliveryTask:
    config: dict
    checkpoint: DeliveryCheckpoint = field(default_factory=DeliveryCheckpoint)
    device: tuple[str, int] | None = None


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.task = None
        self.state = self._new_state()
        self.delivery_stats = DailyDeliveryStats(DELIVERY_STATS_PATH)

    @staticmethod
    def _new_state(limit=0):
        return dict(running=bool(limit), stopping=False, done=0, limit=limit,
                    can_resume=False,
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

    def _run(self, task, resuming):
        result = 'complete'
        try:
            self.delivery_stats.prepare()
            cfg = task.config
            device_cfg = cfg['device'].copy()
            if task.device is not None:
                device_cfg['serial'], device_cfg['port'] = task.device
            adb = AdbClient(device_cfg, stopped=self.stop_event)
            serial = adb.connect()
            task.device = (serial, adb.port)
            with self.lock:
                self.state['device'] = f'{serial} · {adb.port}'
            if resuming:
                self.log('正在继续本次投递任务')
            self.log(f'手机已连接：{serial} · 端口 {adb.port}')
            use_component = device_cfg['ui_reader'] == 'component'
            self.log('控件读取方式：' + ('手机组件' if use_component else 'ADB 命令'))
            with ComponentReader(adb) if use_component else nullcontext():
                Workflow(adb, cfg, log=self.log, progress=self.progress,
                         checkpoint=task.checkpoint, resuming=resuming).run()
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
                used = self.state['done'] + task.checkpoint.unconfirmed
                self.state['can_resume'] = result != 'complete' and used < self.state['limit']

    def _start_worker(self, task, resuming):
        previous = self.state
        self.stop_event.clear()
        if resuming:
            self.state = dict(previous, running=True, stopping=False, result='running', can_resume=False)
        else:
            self.state = self._new_state(task.config['run']['limit'])
        thread = threading.Thread(target=self._run, args=(task, resuming), daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self.state = previous
            raise
        self.thread = thread
        self.task = task
        return True

    def start(self, cfg=None):
        cfg = read_config() if cfg is None else normalize_config(cfg)
        Workflow.validate_config(cfg)
        with self.lock:
            if self.state['running']:
                return False
            return self._start_worker(_DeliveryTask(cfg), resuming=False)

    def resume(self, reader_settings=None, *, reader_device=None):
        with self.lock:
            if not self.state['can_resume']:
                return False
            task = self.task
            if reader_settings is not None:
                config = copy.deepcopy(task.config)
                device = config['device']
                original_device = task.device or (device['serial'], device['port'])
                changes = reader_settings.get('device', {})
                if reader_device in (None, original_device) and 'ui_reader' in changes:
                    device['ui_reader'] = changes['ui_reader']
                task = replace(task, config=normalize_config(config))
            return self._start_worker(task, resuming=True)

    def halt(self):
        with self.lock:
            if self.state['running']:
                self.state['stopping'] = True
                self.stop_event.set()


CONTROLLER = Controller()
