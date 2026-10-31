"""Проверка HTTP-слоя: настоящий сервер на свободном порту, запросы через urllib."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import create_server  # noqa: E402


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "api.db")
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.tmp.cleanup()

    def start_server(self):
        self.server = create_server(self.db, "127.0.0.1", 0)  # порт 0 = любой свободный
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    @staticmethod
    def period(days_back=1, days_forward=1):
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return (f"?start={(now - timedelta(days=days_back)).strftime(fmt)}"
                f"&end={(now + timedelta(days=days_forward)).strftime(fmt)}")


class ApiTests(ApiTestCase):
    def test_index_page_is_served(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("Учёт рабочего времени", resp.read().decode())

    def test_add_employee_and_duplicate(self):
        status, body = self.call("POST", "/api/employees", {"name": "Дарья Образцова"})
        self.assertEqual(status, 201)
        self.assertEqual(body["name"], "Дарья Образцова")
        status, body = self.call("POST", "/api/employees", {"name": "дарья образцова"})
        self.assertEqual(status, 409)
        self.assertIn("уже существует", body["error"])

    def test_empty_employee_name(self):
        status, _ = self.call("POST", "/api/employees", {"name": "  "})
        self.assertEqual(status, 400)

    def test_repeated_timer_start_returns_conflict(self):
        _, emp = self.call("POST", "/api/employees", {"name": "Егор Демов"})
        status, _ = self.call("POST", "/api/timer/start", {"employee_id": emp["id"], "task": "Задача 1"})
        self.assertEqual(status, 201)
        status, body = self.call("POST", "/api/timer/start", {"employee_id": emp["id"], "task": "Задача 2"})
        self.assertEqual(status, 409)
        self.assertIn("уже запущен", body["error"])

    def test_stop_without_timer(self):
        _, emp = self.call("POST", "/api/employees", {"name": "Жанна Пример"})
        status, _ = self.call("POST", "/api/timer/stop", {"employee_id": emp["id"]})
        self.assertEqual(status, 409)

    def test_empty_task_and_unknown_employee(self):
        _, emp = self.call("POST", "/api/employees", {"name": "Зоя Образцова"})
        self.assertEqual(self.call("POST", "/api/timer/start", {"employee_id": emp["id"], "task": ""})[0], 400)
        self.assertEqual(self.call("POST", "/api/timer/start", {"employee_id": 12345, "task": "x"})[0], 404)
        self.assertEqual(self.call("POST", "/api/timer/start", {"employee_id": "abc", "task": "x"})[0], 400)

    def test_empty_report(self):
        """Пустая база и пустой период — 200 и нулевой итог, а не ошибка."""
        status, report = self.call("GET", "/api/report" + self.period())
        self.assertEqual(status, 200)
        self.assertEqual(report, {"employees": [], "total_seconds": 0, "entries_count": 0})
        self.call("POST", "/api/employees", {"name": "Иван Заглушкин"})
        _, report = self.call("GET", "/api/report" + self.period(10, -5))  # период в прошлом
        self.assertEqual(report["total_seconds"], 0)
        self.assertEqual(report["employees"][0]["entries_count"], 0)

    def test_report_requires_valid_period(self):
        self.assertEqual(self.call("GET", "/api/report")[0], 400)
        self.assertEqual(self.call("GET", "/api/report?start=2026-03-05&end=2026-03-01")[0], 400)

    def test_full_flow_and_persistence_across_server_restart(self):
        _, emp = self.call("POST", "/api/employees", {"name": "Клара Сэмплова"})
        self.call("POST", "/api/timer/start", {"employee_id": emp["id"], "task": "Код-ревью"})
        self.call("POST", "/api/timer/stop", {"employee_id": emp["id"]})
        self.call("POST", "/api/timer/start", {"employee_id": emp["id"], "task": "Остался включённым"})

        self.stop_server()
        self.start_server()  # новый процесс-сервер, тот же файл БД

        _, employees = self.call("GET", "/api/employees")
        self.assertEqual([e["name"] for e in employees], ["Клара Сэмплова"])
        self.assertEqual(employees[0]["running_task"], "Остался включённым")
        _, entries = self.call("GET", "/api/entries" + self.period())
        self.assertEqual({e["task"] for e in entries}, {"Код-ревью", "Остался включённым"})

    def test_invalid_json_and_unknown_route(self):
        self.assertEqual(self.call("POST", "/api/employees", raw="{не json".encode("utf-8"))[0], 400)
        self.assertEqual(self.call("POST", "/api/employees", raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.call("POST", "/api/nope", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
