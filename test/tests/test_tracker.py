import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tracker import (ConflictError, NotFoundError, TimeTracker,  # noqa: E402
                     ValidationError, format_duration)

T0 = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
DAY_START = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)
DAY_END = DAY_START + timedelta(days=1)


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.tracker = TimeTracker(self.db)
        self.anna = self.tracker.add_employee("Анна Тестова")["id"]
        self.boris = self.tracker.add_employee("Борис Примеров")["id"]

    def tearDown(self):
        self.tmp.cleanup()


class EmployeeTests(TrackerTestCase):
    def test_add_and_list(self):
        names = [e["name"] for e in self.tracker.list_employees()]
        self.assertEqual(names, ["Анна Тестова", "Борис Примеров"])

    def test_empty_name_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValidationError):
                self.tracker.add_employee(bad)

    def test_duplicate_name_rejected_case_insensitively(self):
        with self.assertRaises(ConflictError):
            self.tracker.add_employee("анна тестова")

    def test_name_is_trimmed(self):
        self.assertEqual(self.tracker.add_employee("  Вера  ")["name"], "Вера")


class TimerTests(TrackerTestCase):
    def test_start_and_stop_records_duration(self):
        self.tracker.start_timer(self.anna, "Написать отчёт", now=T0)
        self.tracker.stop_timer(self.anna, now=T0 + timedelta(minutes=90))
        [entry] = self.tracker.list_entries(DAY_START, DAY_END)
        self.assertEqual(entry["task"], "Написать отчёт")
        self.assertEqual(entry["duration_seconds"], 5400)
        self.assertFalse(entry["running"])

    def test_repeated_start_is_rejected(self):
        """Повторный запуск при уже идущем таймере — конфликт, а не вторая запись."""
        self.tracker.start_timer(self.anna, "Первая задача", now=T0)
        with self.assertRaises(ConflictError):
            self.tracker.start_timer(self.anna, "Вторая задача", now=T0 + timedelta(minutes=5))
        entries = self.tracker.list_entries(DAY_START, DAY_END, employee_id=self.anna, now=T0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["task"], "Первая задача")

    def test_start_allowed_again_after_stop(self):
        self.tracker.start_timer(self.anna, "A", now=T0)
        self.tracker.stop_timer(self.anna, now=T0 + timedelta(minutes=10))
        self.tracker.start_timer(self.anna, "B", now=T0 + timedelta(minutes=20))
        self.assertEqual(len(self.tracker.list_entries(DAY_START, DAY_END)), 2)

    def test_different_employees_can_run_timers_simultaneously(self):
        self.tracker.start_timer(self.anna, "A", now=T0)
        self.tracker.start_timer(self.boris, "B", now=T0)
        running = [e for e in self.tracker.list_employees() if e["running_entry_id"]]
        self.assertEqual(len(running), 2)

    def test_stop_without_running_timer(self):
        with self.assertRaises(ConflictError):
            self.tracker.stop_timer(self.anna)

    def test_double_stop(self):
        self.tracker.start_timer(self.anna, "A", now=T0)
        self.tracker.stop_timer(self.anna, now=T0 + timedelta(minutes=1))
        with self.assertRaises(ConflictError):
            self.tracker.stop_timer(self.anna, now=T0 + timedelta(minutes=2))

    def test_empty_task_rejected_and_nothing_saved(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValidationError):
                self.tracker.start_timer(self.anna, bad, now=T0)
        self.assertEqual(self.tracker.list_entries(DAY_START, DAY_END), [])

    def test_unknown_employee(self):
        with self.assertRaises(NotFoundError):
            self.tracker.start_timer(999, "Задача")
        with self.assertRaises(NotFoundError):
            self.tracker.stop_timer(999)

    def test_stop_before_start_rejected(self):
        self.tracker.start_timer(self.anna, "A", now=T0)
        with self.assertRaises(ValidationError):
            self.tracker.stop_timer(self.anna, now=T0 - timedelta(minutes=1))
        # таймер должен остаться запущенным
        self.assertTrue(self.tracker.list_entries(DAY_START, DAY_END, now=T0)[0]["running"])


class ReportTests(TrackerTestCase):
    def test_empty_report_lists_everyone_with_zero(self):
        """Пустой отчёт: записей нет, но структура корректна и итог = 0."""
        report = self.tracker.report(DAY_START, DAY_END)
        self.assertEqual(report["total_seconds"], 0)
        self.assertEqual(report["entries_count"], 0)
        self.assertEqual([r["total_seconds"] for r in report["employees"]], [0, 0])

    def test_report_with_no_employees_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = TimeTracker(os.path.join(tmp, "empty.db"))
            self.assertEqual(empty.report(DAY_START, DAY_END),
                             {"employees": [], "total_seconds": 0, "entries_count": 0})

    def test_totals_per_employee(self):
        self.tracker.start_timer(self.anna, "A1", now=T0)
        self.tracker.stop_timer(self.anna, now=T0 + timedelta(hours=1))
        self.tracker.start_timer(self.anna, "A2", now=T0 + timedelta(hours=2))
        self.tracker.stop_timer(self.anna, now=T0 + timedelta(hours=2, minutes=30))
        self.tracker.start_timer(self.boris, "B1", now=T0)
        self.tracker.stop_timer(self.boris, now=T0 + timedelta(minutes=15))
        report = self.tracker.report(DAY_START, DAY_END)
        by_name = {r["employee"]: r for r in report["employees"]}
        self.assertEqual(by_name["Анна Тестова"]["total_seconds"], 5400)
        self.assertEqual(by_name["Анна Тестова"]["entries_count"], 2)
        self.assertEqual(by_name["Борис Примеров"]["total_seconds"], 900)
        self.assertEqual(report["total_seconds"], 6300)

    def test_period_start_inclusive_end_exclusive(self):
        self.tracker.start_timer(self.anna, "на границе начала", now=DAY_START)
        self.tracker.stop_timer(self.anna, now=DAY_START + timedelta(minutes=5))
        self.tracker.start_timer(self.anna, "на границе конца", now=DAY_END)
        self.tracker.stop_timer(self.anna, now=DAY_END + timedelta(minutes=5))
        tasks = [e["task"] for e in self.tracker.list_entries(DAY_START, DAY_END)]
        self.assertEqual(tasks, ["на границе начала"])

    def test_entries_outside_period_are_excluded(self):
        self.tracker.start_timer(self.anna, "вчера", now=T0 - timedelta(days=1))
        self.tracker.stop_timer(self.anna, now=T0 - timedelta(days=1) + timedelta(hours=1))
        report = self.tracker.report(DAY_START, DAY_END)
        self.assertEqual(report["entries_count"], 0)

    def test_running_timer_counts_until_now(self):
        self.tracker.start_timer(self.anna, "идёт", now=T0)
        report = self.tracker.report(DAY_START, DAY_END, now=T0 + timedelta(minutes=45))
        self.assertEqual(report["total_seconds"], 2700)
        [entry] = self.tracker.list_entries(DAY_START, DAY_END, now=T0 + timedelta(minutes=45))
        self.assertTrue(entry["running"])

    def test_invalid_period(self):
        with self.assertRaises(ValidationError):
            self.tracker.report(DAY_END, DAY_START)
        with self.assertRaises(ValidationError):
            self.tracker.report(DAY_START, DAY_START)
        with self.assertRaises(ValidationError):
            self.tracker.report("не дата", "2026-03-03")
        with self.assertRaises(ValidationError):
            self.tracker.report("", "")

    def test_offset_in_input_is_respected(self):
        """Дата с часовым поясом Бишкека (+06:00) корректно приводится к UTC."""
        self.tracker.start_timer(self.anna, "утро", now=datetime(2026, 3, 2, 2, 0, tzinfo=timezone.utc))
        self.tracker.stop_timer(self.anna, now=datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc))
        # 2026-03-02 08:00+06:00 == 02:00 UTC — запись попадает в период ровно на границе
        entries = self.tracker.list_entries("2026-03-02T08:00:00+06:00", "2026-03-02T09:00:00+06:00")
        self.assertEqual(len(entries), 1)


class PersistenceTests(TrackerTestCase):
    def test_data_survives_restart(self):
        self.tracker.start_timer(self.anna, "Завершённая", now=T0)
        self.tracker.stop_timer(self.anna, now=T0 + timedelta(hours=1))
        self.tracker.start_timer(self.boris, "Незавершённая", now=T0)

        restarted = TimeTracker(self.db)  # «перезапуск приложения»
        self.assertEqual(len(restarted.list_employees()), 2)
        report = restarted.report(DAY_START, DAY_END, now=T0 + timedelta(hours=2))
        by_name = {r["employee"]: r["total_seconds"] for r in report["employees"]}
        self.assertEqual(by_name["Анна Тестова"], 3600)
        self.assertEqual(by_name["Борис Примеров"], 7200)  # таймер продолжил идти
        with self.assertRaises(ConflictError):  # и правило «один таймер» всё ещё действует
            restarted.start_timer(self.boris, "ещё одна", now=T0 + timedelta(hours=3))


class FormatTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(0), "0:00:00")
        self.assertEqual(format_duration(3661), "1:01:01")
        self.assertEqual(format_duration(100 * 3600), "100:00:00")


if __name__ == "__main__":
    unittest.main()
