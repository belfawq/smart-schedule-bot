"""
Заміри швидкодії запитів до бази даних (для підрозділу 3.2).

Створюється окрема тимчасова база з синтетичними даними (робоча база не змінюється),
у якій вимірюється час основних запитів бота — з індексами і без них.

Запуск:
  python tests/bench_db.py
  python tests/bench_db.py --groups 50 --days 180
"""
import argparse
import random
import time
from datetime import date, timedelta

from common import RESULTS, print_table, save_csv, stats_ms

import database as db

SUBJECTS = ["Об'єктно-орієнтоване програмування", "Операційні системи", "Вища математика",
            "Дискретна математика", "Англійська мова", "Основи програмування мовою Python",
            "Бази даних", "Основи національного спротиву"]
TIMES = [("08:30", "09:50"), ("10:10", "11:30"), ("12:00", "13:20"), ("13:30", "14:50")]
RUNS = 500


def fill(groups: int, days: int, start: date):
    """Синтетичні дані: groups груп × days днів × 2–4 пари на день + домашні завдання."""
    rnd = random.Random(42)
    group_ids = []
    for g in range(groups):
        gid = db.create_group(f"ГР-{g + 1}")
        group_ids.append(gid)
        data = {"schedule": []}
        for d in range(days):
            day = start + timedelta(days=d)
            if day.weekday() >= 5:
                continue
            subjects = [{"name": rnd.choice(SUBJECTS), "time": t[0], "end_time": t[1],
                         "type": rnd.choice(["lecture", "practice", "lab"]),
                         "audience": f"ауд. {rnd.randint(1, 320)}", "teacher": None}
                        for t in TIMES[:rnd.randint(2, 4)]]
            data["schedule"].append({"date": day.isoformat(), "subjects": subjects})
        db.replace_lessons_range(data, gid, start, start + timedelta(days=days))
        for _ in range(days // 3):
            db.add_homework(gid, rnd.choice(SUBJECTS), start + timedelta(days=rnd.randint(0, days)),
                            "Тестове завдання", None)
    return group_ids


def measure(label, fn, rows):
    times = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    s = stats_ms(times)
    rows.append([label, s["n"], s["avg"], s["median"], s["max"]])


def query_plan(sql, params):
    with db.get_connection() as conn:
        return "; ".join(r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", type=int, default=20)
    ap.add_argument("--days", type=int, default=120)
    args = ap.parse_args()

    tmp = RESULTS / "bench_db.db"
    tmp.unlink(missing_ok=True)
    db.DB_NAME = str(tmp)
    db.init_db()

    start = date(2026, 9, 1)
    print(f"Заповнення: {args.groups} груп × {args.days} днів...")
    t0 = time.perf_counter()
    group_ids = fill(args.groups, args.days, start)
    with db.get_connection() as conn:
        n_lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        n_hw = conn.execute("SELECT COUNT(*) FROM homework").fetchone()[0]
    print(f"Занять: {n_lessons}, домашніх завдань: {n_hw} "
          f"(заповнення {time.perf_counter() - t0:.1f} с)")

    rnd = random.Random(1)
    pick = lambda: (rnd.choice(group_ids), start + timedelta(days=rnd.randint(0, args.days - 1)))

    def run_all(suffix, rows):
        measure(f"Заняття на день (/today){suffix}",
                lambda: db.get_lessons(*pick()), rows)
        measure(f"Заняття на тиждень (/week){suffix}",
                lambda: (lambda g, d: db.get_lessons_range(g, d, d + timedelta(days=6)))(*pick()), rows)
        measure(f"Наступна пара з предмета{suffix}",
                lambda: (lambda g, d: db.get_next_lesson_date(g, rnd.choice(SUBJECTS), d))(*pick()), rows)
        measure(f"ДЗ групи на дату (розсилка){suffix}",
                lambda: db.get_homework(*pick()), rows)

    rows = []
    run_all("", rows)
    plan_with = query_plan("SELECT * FROM lessons WHERE group_id = ? AND lesson_date = ?",
                           (1, start.isoformat()))

    # Ті самі запити без індексів — щоб показати, навіщо вони потрібні
    with db.get_connection() as conn:
        conn.execute("DROP INDEX idx_lessons_group_date")
        conn.execute("DROP INDEX idx_homework_group_date")
    run_all(" — без індексу", rows)
    plan_without = query_plan("SELECT * FROM lessons WHERE group_id = ? AND lesson_date = ?",
                              (1, start.isoformat()))

    headers = ["Запит", "Повторів", "Середнє, мс", "Медіана, мс", "Макс, мс"]
    print()
    print_table(headers, rows, digits=3)
    save_csv("db_speed", headers, rows, digits=3)
    print(f"\nПлан запиту з індексом:  {plan_with}")
    print(f"План запиту без індексу: {plan_without}")
    try:
        tmp.unlink(missing_ok=True)
    except PermissionError:
        pass


if __name__ == "__main__":
    main()
