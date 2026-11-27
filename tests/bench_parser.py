"""
Заміри швидкодії модуля вебпарсингу АСУ «ВНЗ» (для підрозділу 3.2).

Що вимірюється:
  1) запит довідника факультетів (GetStudentScheduleFiltersData);
  2) запит розкладу групи на 14 днів (GetScheduleDataX);
  3) нормалізація отриманих рядків;
  4) запис у базу (на тимчасовій копії бази, робоча база не змінюється);
  5) повний цикл синхронізації (запит + нормалізація + запис).

Запуск (з кореня проєкту, бот можна не зупиняти):
  python tests/bench_parser.py
  python tests/bench_parser.py --runs 10 --group ATN9U1BNA485
"""
import argparse
import asyncio
import time
from datetime import timedelta

from common import RESULTS, print_table, save_csv, stats_ms

import clock
import database as db
from parser import fetch_filters, fetch_rows, rows_to_schedule

PAUSE = 1.0  # пауза між запитами, щоб не навантажувати сервер АСУ «ВНЗ»


async def timed(coro_factory, runs):
    times, result = [], None
    for i in range(runs):
        t0 = time.perf_counter()
        result = await coro_factory()
        times.append(time.perf_counter() - t0)
        if i < runs - 1:
            await asyncio.sleep(PAUSE)
    return times, result


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10, help="кількість повторів кожного запиту")
    ap.add_argument("--group", help="aStudyGroupID (за замовчуванням — з бази бота)")
    ap.add_argument("--days", type=int, default=14, help="період розкладу, днів")
    args = ap.parse_args()

    site_id = args.group
    if not site_id:
        groups = db.get_groups_for_sync()
        if not groups:
            print("У базі немає групи, прив'язаної до АСУ «ВНЗ». Вкажи --group <aStudyGroupID>")
            return
        site_id = groups[0]["site_group_id"]
        print(f"Група з бази: {groups[0]['code']} ({site_id})")

    start = clock.today()
    end = start + timedelta(days=args.days)
    rows_out = []

    print(f"\n1) Довідник факультетів, {args.runs} запитів...")
    t, filters = await timed(fetch_filters, args.runs)
    s = stats_ms(t)
    rows_out.append(["Запит довідника (GetStudentScheduleFiltersData)", s["n"], s["min"], s["avg"],
                     s["median"], s["max"]])
    print(f"   факультетів: {len(filters['faculties'])}, курсів: {len(filters['courses'])}")

    print(f"2) Розклад групи на {args.days} днів, {args.runs} запитів...")
    t, raw = await timed(lambda: fetch_rows(site_id, start, end), args.runs)
    s = stats_ms(t)
    rows_out.append([f"Запит розкладу на {args.days} днів (GetScheduleDataX)", s["n"], s["min"],
                     s["avg"], s["median"], s["max"]])

    print("3) Нормалізація, 1000 повторів...")
    t = []
    for _ in range(1000):
        t0 = time.perf_counter()
        data = rows_to_schedule(raw)
        t.append(time.perf_counter() - t0)
    s = stats_ms(t)
    rows_out.append(["Нормалізація рядків", s["n"], s["min"], s["avg"], s["median"], s["max"]])
    lessons = sum(len(d["subjects"]) for d in data["schedule"])
    print(f"   рядків від сервера: {len(raw)}, занять після нормалізації: {lessons}, "
          f"днів із заняттями: {len(data['schedule'])}")

    print("4) Запис у базу (тимчасова копія), 100 повторів...")
    tmp = RESULTS / "bench_parser.db"  # окрема тимчасова база, робоча не змінюється
    tmp.unlink(missing_ok=True)
    real_db, db.DB_NAME = db.DB_NAME, str(tmp)
    db.init_db()
    group_id = db.create_group("BENCH-GROUP")
    t = []
    for _ in range(100):
        t0 = time.perf_counter()
        db.replace_lessons_range(data, group_id, start, end, source="web")
        t.append(time.perf_counter() - t0)
    s = stats_ms(t)
    rows_out.append(["Запис у базу (заміна за період)", s["n"], s["min"], s["avg"], s["median"],
                     s["max"]])

    print(f"5) Повний цикл синхронізації, {args.runs} повторів...")
    async def full_cycle():
        r = await fetch_rows(site_id, start, end)
        db.replace_lessons_range(rows_to_schedule(r), group_id, start, end, source="web")
    t, _ = await timed(full_cycle, args.runs)
    s = stats_ms(t)
    rows_out.append(["Повний цикл синхронізації", s["n"], s["min"], s["avg"], s["median"], s["max"]])
    db.DB_NAME = real_db
    try:
        tmp.unlink(missing_ok=True)
    except PermissionError:
        pass

    headers = ["Етап", "Повторів", "Мін, мс", "Середнє, мс", "Медіана, мс", "Макс, мс"]
    print()
    print_table(headers, rows_out)
    save_csv("parser_speed", headers, rows_out)


if __name__ == "__main__":
    asyncio.run(main())
