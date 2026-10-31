"""Ядро учёта рабочего времени: сотрудники, таймеры, записи, отчёты.

Хранилище — SQLite (файл), только стандартная библиотека.
Все моменты времени хранятся в UTC в формате ``YYYY-MM-DDTHH:MM:SSZ``;
такой формат сортируется как обычная строка, поэтому сравнения в SQL корректны.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    -- Ключ уникальности: имя в нормализованном регистре. COLLATE NOCASE в SQLite
    -- работает только для ASCII и не отличил бы «Анна» от «анна», поэтому
    -- регистр сворачиваем в Python через casefold().
    name_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    task        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    stopped_at  TEXT
);
-- У одного сотрудника не может быть двух активных таймеров одновременно.
-- Правило держится на уровне БД, а не только в коде: так оно переживёт
-- гонки между запросами.
CREATE UNIQUE INDEX IF NOT EXISTS one_running_timer
    ON entries(employee_id) WHERE stopped_at IS NULL;
CREATE INDEX IF NOT EXISTS entries_by_start ON entries(started_at);
"""


class TrackerError(Exception):
    """Базовая ошибка предметной области."""


class ValidationError(TrackerError):
    """Некорректные входные данные (400)."""


class NotFoundError(TrackerError):
    """Сотрудник или запись не найдены (404)."""


class ConflictError(TrackerError):
    """Операция конфликтует с текущим состоянием (409)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_ts(moment: datetime) -> str:
    """datetime -> строка UTC. Наивные datetime считаются UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(value: str | datetime) -> datetime:
    """Принимает ISO 8601 (с 'Z' или смещением) или datetime, возвращает aware UTC."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValidationError(f"Некорректная дата/время: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0)


class TimeTracker:
    def __init__(self, db_path: str = "timetracker.db"):
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        # Новое соединение на каждую операцию: просто и безопасно для потоков.
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:  # commit / rollback
                yield conn
        finally:
            conn.close()

    # ---------- сотрудники ----------

    def add_employee(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValidationError("Имя сотрудника не может быть пустым")
        if len(name) > 100:
            raise ValidationError("Имя слишком длинное (максимум 100 символов)")
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO employees(name, name_key) VALUES (?, ?)",
                    (name, " ".join(name.split()).casefold()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"Сотрудник «{name}» уже существует") from exc
            return {"id": cur.lastrowid, "name": name}

    def list_employees(self) -> list[dict]:
        """Сотрудники с признаком активного таймера."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT e.id, e.name, r.id AS running_entry_id,
                       r.task AS running_task, r.started_at AS running_since
                FROM employees e
                LEFT JOIN entries r ON r.employee_id = e.id AND r.stopped_at IS NULL
                """
            ).fetchall()
        # Сортировка в Python по той же причине, что и name_key (кириллица).
        return sorted((dict(r) for r in rows), key=lambda e: e["name"].casefold())

    def _require_employee(self, conn, employee_id: int) -> None:
        found = conn.execute(
            "SELECT 1 FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        if not found:
            raise NotFoundError(f"Сотрудник с id={employee_id} не найден")

    # ---------- таймер ----------

    def start_timer(self, employee_id: int, task: str, now: datetime | None = None) -> dict:
        task = (task or "").strip()
        if not task:
            raise ValidationError("Опишите задачу перед запуском таймера")
        if len(task) > 300:
            raise ValidationError("Описание задачи слишком длинное (максимум 300 символов)")
        started = to_ts(now or utcnow())
        with self._connect() as conn:
            self._require_employee(conn, employee_id)
            try:
                cur = conn.execute(
                    "INSERT INTO entries(employee_id, task, started_at) VALUES (?, ?, ?)",
                    (employee_id, task, started),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "У сотрудника уже запущен таймер — сначала остановите его"
                ) from exc
            return {"id": cur.lastrowid, "employee_id": employee_id,
                    "task": task, "started_at": started, "stopped_at": None}

    def stop_timer(self, employee_id: int, now: datetime | None = None) -> dict:
        stopped = to_ts(now or utcnow())
        with self._connect() as conn:
            self._require_employee(conn, employee_id)
            row = conn.execute(
                "SELECT id, task, started_at FROM entries "
                "WHERE employee_id = ? AND stopped_at IS NULL",
                (employee_id,),
            ).fetchone()
            if row is None:
                raise ConflictError("У сотрудника нет запущенного таймера")
            if stopped < row["started_at"]:
                raise ValidationError("Время остановки раньше времени запуска")
            conn.execute("UPDATE entries SET stopped_at = ? WHERE id = ?", (stopped, row["id"]))
            return {"id": row["id"], "employee_id": employee_id, "task": row["task"],
                    "started_at": row["started_at"], "stopped_at": stopped}

    # ---------- записи и отчёты ----------

    @staticmethod
    def _period(start, end) -> tuple[str, str]:
        """Период [start, end): начало включительно, конец исключительно."""
        start_dt, end_dt = parse_ts(start), parse_ts(end)
        if end_dt <= start_dt:
            raise ValidationError("Конец периода должен быть позже начала")
        return to_ts(start_dt), to_ts(end_dt)

    @staticmethod
    def _duration(started_at: str, stopped_at: str | None, now: datetime) -> int:
        end = parse_ts(stopped_at) if stopped_at else now
        return max(0, int((end - parse_ts(started_at)).total_seconds()))

    def list_entries(self, start, end, employee_id: int | None = None,
                     now: datetime | None = None) -> list[dict]:
        """Записи, которые НАЧАЛИСЬ в периоде [start, end). Активный таймер
        считается до текущего момента."""
        start_ts, end_ts = self._period(start, end)
        now = parse_ts(now) if now else utcnow()
        query = (
            "SELECT en.id, en.employee_id, e.name AS employee, en.task, "
            "en.started_at, en.stopped_at FROM entries en "
            "JOIN employees e ON e.id = en.employee_id "
            "WHERE en.started_at >= ? AND en.started_at < ?"
        )
        params: list = [start_ts, end_ts]
        if employee_id is not None:
            query += " AND en.employee_id = ?"
            params.append(employee_id)
        query += " ORDER BY en.started_at DESC, en.id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["running"] = item["stopped_at"] is None
            item["duration_seconds"] = self._duration(item["started_at"], item["stopped_at"], now)
            result.append(item)
        return result

    def report(self, start, end, now: datetime | None = None) -> dict:
        """Итог по каждому сотруднику за период. Сотрудники без записей
        присутствуют с нулём — пустой отчёт остаётся осмысленным."""
        entries = self.list_entries(start, end, now=now)
        totals = {e["id"]: {"employee_id": e["id"], "employee": e["name"],
                            "total_seconds": 0, "entries_count": 0}
                  for e in self.list_employees()}
        for item in entries:
            row = totals[item["employee_id"]]
            row["total_seconds"] += item["duration_seconds"]
            row["entries_count"] += 1
        rows = sorted(totals.values(), key=lambda r: (-r["total_seconds"], r["employee"].casefold()))
        return {
            "employees": rows,
            "total_seconds": sum(r["total_seconds"] for r in rows),
            "entries_count": len(entries),
        }


def format_duration(seconds: int) -> str:
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"
