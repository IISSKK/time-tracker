"""Заполняет БД вымышленными демо-данными: python seed.py [--db timetracker.db]"""

import argparse
from datetime import timedelta

from tracker import ConflictError, TimeTracker, utcnow

# (имя, [(дней назад, час начала, минут, задача), ...]) — все данные вымышленные
DEMO = [
    ("Анна Тестова", [(0, 9, 95, "Вёрстка главной страницы"), (1, 10, 120, "Код-ревью"),
                      (2, 14, 45, "Планёрка")]),
    ("Борис Примеров", [(0, 10, 60, "Написание тестов"), (1, 11, 150, "Исправление багов"),
                        (3, 9, 30, "Разбор заявок")]),
    ("Вера Образцова", [(1, 9, 80, "Подготовка отчёта"), (2, 13, 100, "Интервью с клиентом")]),
    ("Глеб Демов", []),  # сотрудник без записей — для примера пустой строки в отчёте
]


def seed(db_path: str) -> None:
    tracker = TimeTracker(db_path)
    today = utcnow().replace(hour=0, minute=0, second=0)
    for name, entries in DEMO:
        try:
            employee_id = tracker.add_employee(name)["id"]
        except ConflictError:
            print(f"пропуск: {name} уже есть")
            continue
        for days_ago, hour, minutes, task in entries:
            start = today - timedelta(days=days_ago) + timedelta(hours=hour)
            tracker.start_timer(employee_id, task, now=start)
            tracker.stop_timer(employee_id, now=start + timedelta(minutes=minutes))
    print("Демо-данные добавлены")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="timetracker.db")
    seed(parser.parse_args().db)
