"""Спільні функції для скриптів тестування і замірів (розділ 3 курсової)."""
import csv
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).resolve().parent / "results"
RESULTS.mkdir(exist_ok=True)

# Модулі бота лежать у корені репозиторію
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # щоб .env і database.db знаходились як під час звичайного запуску

# Українські літери в консолі Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def stats_ms(samples_s: list[float]) -> dict:
    """Статистика за вибіркою часів (у секундах) -> мілісекунди."""
    ms = [s * 1000 for s in samples_s]
    return {
        "n": len(ms),
        "min": min(ms),
        "avg": statistics.mean(ms),
        "median": statistics.median(ms),
        "max": max(ms),
        "p95": sorted(ms)[max(0, round(0.95 * len(ms)) - 1)],
        "stdev": statistics.stdev(ms) if len(ms) > 1 else 0.0,
    }


def fmt(x, digits=1) -> str:
    """Число з комою як десятковим роздільником (для таблиць українською)."""
    if isinstance(x, float):
        return f"{x:.{digits}f}".replace(".", ",")
    return str(x)


def print_table(headers: list[str], rows: list[list], digits: int = 1):
    rows = [[fmt(c, digits) for c in r] for r in rows]
    widths = [max(len(str(h)), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    line = "+".join("-" * (w + 2) for w in widths)
    print(line)
    print(" | ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    print(line)
    for r in rows:
        print(" | ".join(c.ljust(w) for c, w in zip(r, widths)))
    print(line)


def save_csv(name: str, headers: list[str], rows: list[list], digits: int = 2) -> Path:
    """Зберігає таблицю в tests/results/<name>.csv (відкривається в Excel)."""
    path = RESULTS / f"{name}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(headers)
        for r in rows:
            w.writerow([fmt(c, digits) for c in r])
    print(f"Збережено: {path}")
    return path
