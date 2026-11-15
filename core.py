"""GUI-independent task controller and configuration. No browser service."""
import os
import copy
import json
import sys
import threading
from pathlib import Path
from engine.adb_client import AdbClient
from engine.workflow import Workflow

BASE_DIR = (
    Path(os.environ.get("APPDATA") or Path.home()) / "BossApply"
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
BASE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULTS = {
    'device': {'path': 'auto', 'port': 0, 'serial': 'auto', 'click_interval': 0.15},
    'run': {'limit': 20, 'timeout': 18, 'scroll_fraction': 0.145, 'max_scroll_attempts': 20},
    'actions': {'image': True, 'quick_phrase': False},
    'image': {'album': 'My Documents', 'index': 1, 'original': True},
}


def read_config():
    if not CONFIG_PATH.is_file():
        write_config(copy.deepcopy(DEFAULTS))
    cfg = copy.deepcopy(DEFAULTS)
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    for section in cfg:
        cfg[section].update(data.get(section, {}))
    cfg['run']['limit'] = min(150, max(1, int(cfg['run']['limit'])))
    return cfg


def write_config(cfg):
    cfg = copy.deepcopy(cfg)
    cfg['run']['limit'] = min(150, max(1, int(cfg['run']['limit'])))
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.state = dict(running=False, done=0, limit=0, logs=[], device='', result='idle')

    def snapshot(self):
        with self.lock:
            return {**self.state, 'logs': list(self.state['logs'])}

    def log(self, line):
        print(line, flush=True)
        with self.lock:
            self.state['logs'].append(line)
            self.state['logs'] = self.state['logs'][-120:]

    def progress(self, n, title):
        with self.lock:
            self.state['done'] = n

    def _run(self, cfg):
        result = 'complete'
        try:
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
                self.state['result'] = result

    def start(self):
        cfg = read_config()
        with self.lock:
            if self.state['running']:
                return False
            self.state = dict(running=True, done=0, limit=cfg['run']['limit'], logs=[],
                              device='', result='running')
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self.thread.start()
        return True

    def halt(self):
        self.stop_event.set()


CONTROLLER = Controller()
