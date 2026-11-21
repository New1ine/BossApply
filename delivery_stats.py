"""Persist confirmed deliveries by the computer's local calendar date."""
import sqlite3
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path


class DeliveryStatsError(RuntimeError):
    """The local delivery totals could not be read or written."""


class DailyDeliveryStats:
    def __init__(self, path):
        self.path = Path(path).absolute()

    @contextmanager
    def _connect(self, *, reading=False, create=False):
        try:
            if create:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            # Allow SQLite to recover an interrupted transaction on startup.
            mode = 'rwc' if create else 'rw'
            uri = f'{self.path.as_uri()}?mode={mode}'
            # Keep reads from the UI responsive while another process commits.
            timeout = 0.05 if reading else 3
            with closing(sqlite3.connect(uri, uri=True, timeout=timeout)) as db, db:
                yield db
        except (OSError, sqlite3.Error) as exc:
            action = '读取' if reading else '写入'
            raise DeliveryStatsError(f'无法{action}投递统计（{self.path}）：{exc}') from exc

    def prepare(self):
        with self._connect(create=True) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''
                CREATE TABLE IF NOT EXISTS daily_deliveries (
                    day TEXT PRIMARY KEY,
                    success_count INTEGER NOT NULL CHECK (success_count >= 0)
                )
            ''')

    def count_today(self):
        day = date.today().isoformat()
        try:
            self.path.stat()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise DeliveryStatsError(f'无法读取投递统计（{self.path}）：{exc}') from exc
        with self._connect(reading=True) as db:
            row = db.execute('SELECT success_count FROM daily_deliveries WHERE day = ?', (day,)).fetchone()
        return row[0] if row is not None else 0

    def add_successes(self, count):
        day = date.today().isoformat()
        with self._connect() as db:
            db.execute('''
                INSERT INTO daily_deliveries (day, success_count) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET success_count = success_count + excluded.success_count
            ''', (day, count))
