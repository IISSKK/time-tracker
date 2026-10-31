"""HTTP-сервер: JSON API поверх TimeTracker + раздача одной статической страницы.

Запуск:  python server.py [--port 8000] [--db timetracker.db]
"""

from __future__ import annotations

import argparse
import json
import os
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tracker import (ConflictError, NotFoundError, TimeTracker, TrackerError,
                     ValidationError)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 64 * 1024

STATUS_BY_ERROR = {
    ValidationError: HTTPStatus.BAD_REQUEST,
    NotFoundError: HTTPStatus.NOT_FOUND,
    ConflictError: HTTPStatus.CONFLICT,
}


def make_handler(tracker: TimeTracker):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TimeTracker/1.0"

        # ---- вспомогательное ----
        def _send_json(self, payload, status=HTTPStatus.OK):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValidationError("Слишком большое тело запроса")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("Тело запроса — не корректный JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("Ожидался JSON-объект")
            return data

        @staticmethod
        def _employee_id(value) -> int:
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError("employee_id должен быть числом") from exc

        def _dispatch(self, fn):
            try:
                fn()
            except TrackerError as exc:
                status = next((s for cls, s in STATUS_BY_ERROR.items() if isinstance(exc, cls)),
                              HTTPStatus.BAD_REQUEST)
                self._send_json({"error": str(exc)}, status)
            except Exception:  # непредвиденное: не роняем сервер и не светим детали
                traceback.print_exc()  # детали — в лог сервера, клиенту только общий ответ
                self._send_json({"error": "Внутренняя ошибка сервера"},
                                HTTPStatus.INTERNAL_SERVER_ERROR)

        # ---- маршруты ----
        def do_GET(self):
            self._dispatch(self._get)

        def do_POST(self):
            self._dispatch(self._post)

        def _get(self):
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/employees":
                self._send_json(tracker.list_employees())
            elif url.path == "/api/entries":
                employee_id = query.get("employee_id")
                self._send_json(tracker.list_entries(
                    query.get("start", ""), query.get("end", ""),
                    self._employee_id(employee_id) if employee_id else None))
            elif url.path == "/api/report":
                self._send_json(tracker.report(query.get("start", ""), query.get("end", "")))
            else:
                self._send_json({"error": "Не найдено"}, HTTPStatus.NOT_FOUND)

        def _post(self):
            path = urlparse(self.path).path
            data = self._read_json()
            if path == "/api/employees":
                self._send_json(tracker.add_employee(data.get("name", "")), HTTPStatus.CREATED)
            elif path == "/api/timer/start":
                self._send_json(tracker.start_timer(
                    self._employee_id(data.get("employee_id")), data.get("task", "")),
                    HTTPStatus.CREATED)
            elif path == "/api/timer/stop":
                self._send_json(tracker.stop_timer(self._employee_id(data.get("employee_id"))))
            else:
                self._send_json({"error": "Не найдено"}, HTTPStatus.NOT_FOUND)

        def log_message(self, fmt, *args):  # тише в консоли
            pass

    return Handler


def create_server(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(TimeTracker(db_path)))


def main():
    parser = argparse.ArgumentParser(description="Учёт рабочего времени")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--db", default=os.environ.get("TIMETRACKER_DB", "timetracker.db"))
    args = parser.parse_args()
    server = create_server(args.db, args.host, args.port)
    print(f"Открой http://{args.host}:{args.port}  (БД: {args.db})  — Ctrl+C для остановки")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
