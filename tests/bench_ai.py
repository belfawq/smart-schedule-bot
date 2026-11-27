"""
Оцінка точності та швидкодії розпізнавання розкладу (для підрозділу 3.2).

Зразки кладуться в папку tests/samples:
  назва.jpg / назва.png      — одне фото розкладу;
  назва/                     — папка з кількома фото (як альбом у Telegram);
  назва.txt                  — розклад текстом.

Еталон (правильна відповідь) для кожного зразка:
  назва.expected.json        — якщо є, береться з цього файлу;
  інакше з ключем --api      — береться з АСУ «ВНЗ» за ті самі дати (для знімків розкладу з АСУ).

Запуск:
  python tests/bench_ai.py                       # усі зразки, 3 повтори кожного
  python tests/bench_ai.py --api --runs 3
  python tests/bench_ai.py --api --fields subject,end_time   # знімки з застосунку АСУ «ВНЗ»,
                                                             # де видно лише час і назву
"""
import argparse
import asyncio
import json
import time
from datetime import date

from common import RESULTS, ROOT, print_table, save_csv, stats_ms

import database as db
from ai_utils import parse_schedule
from parser import (fetch_schedule, normalize_audience, normalize_subgroup, normalize_type)

SAMPLES = ROOT / "tests" / "samples"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
ALL_FIELDS = ["end_time", "subject", "type", "audience", "teacher"]
PAUSE = 3.0  # пауза між запитами до Gemini (обмеження безкоштовного тарифу)
RETRIES = 3  # повторні спроби, якщо Gemini відповів помилкою (503 — перевантаження)
RETRY_WAIT = 15.0


# ---------- Нормалізація для порівняння ----------

def norm_time(t):
    if not t:
        return None
    t = str(t).strip().replace(".", ":")
    if ":" in t:
        h, m = t.split(":")[:2]
        return f"{int(h):02d}:{int(m):02d}"
    return t


def norm_text(s):
    if not s:
        return None
    s = str(s).casefold()
    for ch in "’ʼ`´":
        s = s.replace(ch, "'")
    s = s.replace(".", ". ")
    return " ".join(s.split()).strip(" .,;_")  # «_» у кінці назви — артефакт сайту


def lessons_of(data) -> dict:
    """{(дата, час початку, підгрупа): заняття} у нормалізованому вигляді."""
    out = {}
    for day in (data or {}).get("schedule", []):
        for sub in day.get("subjects", []):
            subgroup = normalize_subgroup(sub["subgroup"]) if sub.get("subgroup") else ""
            key = (str(day["date"]), norm_time(sub.get("time")), subgroup)
            out[key] = {
                "end_time": norm_time(sub.get("end_time")),
                "subject": norm_text(sub.get("name")),
                "type": normalize_type(sub.get("type") or ""),
                "audience": norm_text(normalize_audience(sub.get("audience") or "")),
                "teacher": norm_text(sub.get("teacher")),
            }
    return out


def compare(expected: dict, got: dict, fields: list[str]) -> dict:
    exp, rec = lessons_of(expected), lessons_of(got)
    matched = [k for k in exp if k in rec]
    correct = total = 0
    per_field = {f: [0, 0] for f in fields}
    errors = []
    for k in matched:
        for f in fields:
            if exp[k][f] is None:
                continue  # поля немає в еталоні — не оцінюємо
            total += 1
            per_field[f][1] += 1
            if exp[k][f] == rec[k][f]:
                correct += 1
                per_field[f][0] += 1
            else:
                errors.append(f"{k[0]} {k[1]} {f}: очікувалось «{exp[k][f]}», отримано «{rec[k][f]}»")
    for k in exp:
        if k not in rec:
            errors.append(f"{k[0]} {k[1]} підгр.{k[2] or '-'}: заняття не розпізнано")
    for k in rec:
        if k not in exp:
            errors.append(f"{k[0]} {k[1]} підгр.{k[2] or '-'}: зайве заняття")
    return {
        "expected": len(exp), "recognized": len(rec), "matched": len(matched),
        "recall": len(matched) / len(exp) if exp else 0.0,
        "precision": len(matched) / len(rec) if rec else 0.0,
        "field_acc": correct / total if total else 1.0,
        "per_field": per_field, "errors": errors,
    }


# ---------- Зразки ----------

def find_samples():
    samples = []
    for p in sorted(SAMPLES.iterdir()):
        if p.name.endswith(".expected.json") or p.name.startswith("."):
            continue
        if p.is_dir():
            images = sorted(x for x in p.iterdir() if x.suffix.lower() in IMAGE_EXT)
            if images:
                samples.append((p.name, "фото ×%d" % len(images), [x.read_bytes() for x in images], True))
        elif p.suffix.lower() in IMAGE_EXT:
            samples.append((p.stem, "фото", [p.read_bytes()], True))
        elif p.suffix.lower() == ".txt":
            samples.append((p.stem, "текст", p.read_text(encoding="utf-8"), False))
    return samples


async def expected_for(name: str, result, use_api: bool):
    path = SAMPLES / f"{name}.expected.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), "файл"
    if not use_api or not result:
        return None, None
    groups = db.get_groups_for_sync()
    if not groups:
        return None, None
    dates = [date.fromisoformat(d["date"]) for d in result["schedule"]]
    truth = await fetch_schedule(groups[0]["site_group_id"], min(dates), max(dates))
    return truth, "АСУ «ВНЗ»"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--api", action="store_true", help="еталон з АСУ «ВНЗ», якщо немає .expected.json")
    ap.add_argument("--fields", default=",".join(ALL_FIELDS),
                    help="поля для оцінки: " + ",".join(ALL_FIELDS))
    args = ap.parse_args()
    fields = [f.strip() for f in args.fields.split(",") if f.strip() in ALL_FIELDS]

    samples = find_samples()
    if not samples:
        print(f"Поклади зразки в {SAMPLES} (фото, папки з фото або .txt)")
        return

    rows, report = [], []
    for name, kind, data, is_image in samples:
        print(f"\n=== {name} ({kind}) ===")
        times, results, failed = [], [], 0
        for i in range(args.runs):
            for attempt in range(1, RETRIES + 1):
                t0 = time.perf_counter()
                try:
                    res = await parse_schedule(data, is_image=is_image)
                except Exception as e:
                    failed += 1
                    print(f"  повтор {i + 1}, спроба {attempt}: помилка сервера ({str(e)[:60]}...)")
                    await asyncio.sleep(RETRY_WAIT)
                    continue
                times.append(time.perf_counter() - t0)
                results.append(res)
                print(f"  повтор {i + 1}: {times[-1]:.2f} с")
                break
            await asyncio.sleep(PAUSE)
        if not results:
            print("  усі спроби невдалі — сервер Gemini перевантажений, спробуй пізніше")
            continue

        expected, source = await expected_for(name, results[0], args.api)
        s = stats_ms(times)
        if expected is None:
            print("  еталону немає — оцінюю лише швидкодію")
            rows.append([name, kind, len(results), failed, s["avg"] / 1000, s["min"] / 1000,
                         s["max"] / 1000, "—", "—", "—", "—"])
            continue

        cmp = [compare(expected, r, fields) for r in results]
        for i, c in enumerate(cmp, start=1):
            print(f"  повтор {i}: знайдено {c['matched']}/{c['expected']}, зайвих {c['recognized'] - c['matched']}, "
                  f"правильних полів {c['field_acc'] * 100:.1f}%")
        avg = lambda key: sum(c[key] for c in cmp) / len(cmp)
        rows.append([name, kind, len(results), failed, s["avg"] / 1000, s["min"] / 1000, s["max"] / 1000,
                     cmp[0]["expected"], avg("recall") * 100, avg("precision") * 100, avg("field_acc") * 100])
        report.append(f"=== {name} ({kind}), еталон: {source} ===")
        for f in fields:
            ok = sum(c["per_field"][f][0] for c in cmp)
            tot = sum(c["per_field"][f][1] for c in cmp)
            if tot:
                report.append(f"  {f}: {ok}/{tot} ({ok / tot * 100:.1f}%)")
        for i, c in enumerate(cmp, start=1):
            report.append(f"  --- повтор {i} ---")
            report += ["  " + e for e in c["errors"]] or ["  помилок немає"]

    headers = ["Зразок", "Тип", "Успішних", "Помилок сервера", "Середній час, с", "Мін, с", "Макс, с",
               "Занять в еталоні",
               "Знайдено занять, %", "Без зайвих, %", "Правильних полів, %"]
    print()
    print_table(headers, rows)
    save_csv("ai_accuracy", headers, rows)
    (RESULTS / "ai_errors.txt").write_text("\n".join(report), encoding="utf-8")
    print(f"Детальний перелік помилок: {RESULTS / 'ai_errors.txt'}")


if __name__ == "__main__":
    asyncio.run(main())