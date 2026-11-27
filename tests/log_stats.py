"""
Час обробки повідомлень реальним ботом за його журналом (для підрозділу 3.2).

aiogram після кожного оновлення пише в журнал рядок виду
  Update id=123 is handled. Duration 85 ms by bot id=...
Скрипт збирає ці значення і рахує середнє, медіану, 95-й перцентиль і максимум.

1) Запусти бота із записом журналу у файл (PowerShell):
     python main.py 2>&1 | Tee-Object -FilePath bot.log
2) Попрацюй з ботом звичайним чином (розклад, ДЗ, меню), потім зупини бота.
3) python tests/log_stats.py bot.log
"""
import re
import sys

from common import print_table, save_csv, stats_ms

PATTERN = re.compile(r"Update id=\d+ is (handled|not handled)\. Duration (\d+) ms")


def main():
    if len(sys.argv) < 2:
        print("Вкажи файл журналу: python tests/log_stats.py bot.log")
        return
    handled, ignored = [], []
    raw = open(sys.argv[1], "rb").read()
    # Tee-Object у Windows PowerShell 5 пише файл в UTF-16, а не в UTF-8
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="ignore")
    else:
        text = raw.decode("utf-8", errors="ignore")
    if True:
        for line in text.splitlines():
            m = PATTERN.search(line)
            if m:
                (handled if m[1] == "handled" else ignored).append(int(m[2]) / 1000)
    if not handled:
        print("У журналі немає рядків «Update id=... is handled. Duration ...»")
        return

    s = stats_ms(handled)
    fast = [t for t in handled if t < 1.0]  # без розпізнавання фото і запитів до АСУ «ВНЗ»
    f = stats_ms(fast) if fast else None
    headers = ["Вибірка", "Кількість", "Середнє, мс", "Медіана, мс", "95-й перцентиль, мс", "Макс, мс"]
    rows = [["Усі оброблені оновлення", s["n"], s["avg"], s["median"], s["p95"], s["max"]]]
    if f:
        rows.append(["Без довгих операцій (< 1 с)", f["n"], f["avg"], f["median"], f["p95"], f["max"]])
    print_table(headers, rows)
    print(f"Необроблених оновлень (бот не реагував): {len(ignored)}")
    save_csv("bot_log_stats", headers, rows)


if __name__ == "__main__":
    main()