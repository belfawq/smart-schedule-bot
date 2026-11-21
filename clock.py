"""Час закладу освіти. Усі дати бота рахуються за київським часом,
незалежно від часового поясу комп'ютера чи сервера, де запущено бота."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()