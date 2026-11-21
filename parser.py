"""
Модуль вебпарсингу розкладу з АСУ «ВНЗ».

Віджет розкладу на сайті закладу освіти та мобільний застосунок «АСУ ВНЗ»
отримують дані з вебсервісу vnz.osvita.net у форматі JSON. Модуль звертається
до цього сервісу напряму, тому не залежить від роботи сайту закладу освіти.
"""
import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime

import aiohttp
from dotenv import load_dotenv

load_dotenv()

VUZ_ID = os.getenv("VUZ_ID")
API_BASE = "https://vnz.osvita.net/WidgetSchedule.asmx"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Без заголовка User-Agent сервер повертає HTML-сторінку з кодом 404,
# а JSON (замість XML) віддає лише за заголовка Content-Type: application/json
HEADERS = {
    "User-Agent": "DueDone-schedule-bot/1.0",
    "Content-Type": "application/json; charset=utf-8",
}

# Тип заняття в АСУ «ВНЗ» записаний по-різному («Лекція», «Практичні заняття»,
# «Лабораторні заняття»...), тому визначаємо його за початком слова
LESSON_TYPE_PREFIXES = [("лек", "lecture"), ("практ", "practice"), ("семін", "practice"),
                        ("лаб", "lab")]


def normalize_type(raw: str):
    low = raw.strip().lower()
    for prefix, code in LESSON_TYPE_PREFIXES:
        if low.startswith(prefix):
            return code
    return raw.strip() or None


def normalize_subgroup(raw: str) -> str:
    """«1 підг.», «Підгрупа: 1а» -> «1», «1а»."""
    m = re.search(r"\d+\s*[а-яґєіїa-z]?(?![а-яґєії])", raw.lower())
    return m.group(0).replace(" ", "") if m else raw.strip()


def normalize_audience(raw: str):
    """
    Приводить аудиторію до вигляду «ауд. 29, корп. 4».
    Розпізнає «ауд. №29, Корпус №4», «ауд. 29 корп. 4», «29 корп. 4», «29»;
    значення без номера («онлайн», «спортзал») залишає без змін.
    """
    raw = raw.strip()
    if not raw:
        return None
    aud = re.search(r"ауд(?:иторія)?\.?\s*№?\s*([^,\s]+)", raw, re.IGNORECASE)
    building = re.search(r"корп(?:ус)?\.?\s*№?\s*([^,\s]+)", raw, re.IGNORECASE)
    if aud is None:
        # номер без слова «ауд.»: «29 корп. 4» або просто «29».
        # Інші записи з номером («43 (ЦК)») залишаємо як є, щоб не втратити пояснення
        number = re.match(r"№?\s*(\d+[\w/-]*)\s*,?\s*(.*)$", raw)
        rest = number.group(2) if number else ""
        if number and (not rest or (building and rest.lower().startswith("корп"))):
            aud = number
    if not aud:
        return raw
    return f"ауд. {aud.group(1)}" + (f", корп. {building.group(1)}" if building else "")


class ScheduleSourceError(Exception):
    """Сервер розкладу недоступний або повернув некоректні дані."""


def _fmt(d: date) -> str:
    return d.strftime("%d.%m.%Y")


async def call_api(method: str, params: dict):
    """
    Викликає метод вебсервісу WidgetSchedule.asmx і повертає вміст поля "d".
    Сервіс ASP.NET (.asmx) очікує значення параметрів у форматі JSON:
    рядки в лапках, відсутнє значення — null, числа й логічні значення без лапок.
    """
    if not VUZ_ID:
        raise ScheduleSourceError("Не вказано VUZ_ID у файлі .env")
    params = {"aVuzID": VUZ_ID, **params}
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, headers=HEADERS) as session:
            async with session.get(f"{API_BASE}/{method}", params=params) as resp:
                if resp.status != 200:
                    raise ScheduleSourceError(f"Сервер розкладу повернув код {resp.status}")
                if "json" not in resp.headers.get("Content-Type", ""):
                    raise ScheduleSourceError("Сервер розкладу повернув не JSON")
                payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ScheduleSourceError(f"Сервер розкладу недоступний: {e}") from e
    except ValueError as e:
        raise ScheduleSourceError("Сервер розкладу повернув не JSON") from e

    if not isinstance(payload, dict) or "d" not in payload:
        raise ScheduleSourceError("Неочікувана структура відповіді сервера")
    return payload["d"]


async def fetch_rows(study_group_id: str, start: date, end: date) -> list[dict]:
    """Завантажує «сирі» рядки розкладу групи за період [start; end]."""
    rows = await call_api("GetScheduleDataX", {
        "aStudyGroupID": f'"{study_group_id}"',
        "aStartDate": f'"{_fmt(start)}"',
        "aEndDate": f'"{_fmt(end)}"',
        "aStudyTypeID": "null",
    })
    if not isinstance(rows, list):
        raise ScheduleSourceError("Неочікувана структура відповіді сервера")
    return rows


# ---------- Довідники для вибору групи ----------

def _pairs(items) -> list[tuple[str, str]]:
    """[{"Key": "...", "Value": "..."}] -> [(key, value)]"""
    return [(str(i["Key"]), str(i["Value"])) for i in (items or [])
            if isinstance(i, dict) and "Key" in i and "Value" in i]


async def fetch_filters() -> dict:
    """Факультети, форми навчання та курси закладу освіти."""
    d = await call_api("GetStudentScheduleFiltersData", {})
    if not isinstance(d, dict):
        raise ScheduleSourceError("Неочікувана структура відповіді сервера")
    return {"faculties": _pairs(d.get("faculties")),
            "forms": _pairs(d.get("educForms")),
            "courses": _pairs(d.get("courses"))}


async def fetch_study_groups(faculty_id: str, form_id: str, course_id: str) -> list[tuple[str, str]]:
    """Академічні групи факультету для заданих форми навчання і курсу: [(id, назва)]."""
    d = await call_api("GetStudyGroups", {
        "aFacultyID": f'"{faculty_id}"',
        "aEducationForm": f'"{form_id}"',
        "aCourse": f'"{course_id}"',
        "aGiveStudyTimes": "false",
    })
    if not isinstance(d, dict):
        raise ScheduleSourceError("Неочікувана структура відповіді сервера")
    return _pairs(d.get("studyGroups"))


def get_subgroup(row: dict):
    """Підгрупа заняття: поле study_subgroup або contingent виду «Підгрупа: 1а»."""
    subgroup = (row.get("study_subgroup") or "").strip()
    contingent = (row.get("contingent") or "").strip()
    if not subgroup and contingent.lower().startswith("підгрупа:"):
        subgroup = contingent.split(":", 1)[1].strip()
    return normalize_subgroup(subgroup) if subgroup else None


def rows_to_schedule(rows: list[dict]) -> dict:
    """
    Перетворює рядки АСУ «ВНЗ» у єдиний формат розкладу бота:
    {"schedule": [{"date": "2026-11-19", "subjects": [...]}, ...]}.
    Цей самий формат повертає модуль розпізнавання (ai_utils.py).
    """
    days = defaultdict(list)
    for row in rows:
        start_time = (row.get("study_time_begin") or "").strip()
        # У назвах дисциплін у джерелі трапляється зайве підкреслення в кінці
        subject = (row.get("discipline") or "").strip().rstrip("_").strip()
        raw_date = (row.get("full_date") or "").strip()
        if not (start_time and subject and raw_date):
            continue  # службові або неповні рядки пропускаємо
        try:
            lesson_date = datetime.strptime(raw_date, "%d.%m.%Y").date()
        except ValueError:
            logging.warning("Некоректна дата в розкладі: %r", raw_date)
            continue

        raw_type = (row.get("study_type") or "").strip()
        subgroup = get_subgroup(row)
        days[lesson_date].append({
            "name": subject,
            "subgroup": subgroup,
            "time": start_time,
            "end_time": (row.get("study_time_end") or "").strip() or None,
            "type": normalize_type(raw_type),
            "teacher": (row.get("employee") or "").strip() or None,
            "audience": normalize_audience(row.get("cabinet") or ""),
        })

    return {"schedule": [
        {"date": d.isoformat(), "subjects": sorted(subjects, key=lambda s: s["time"])}
        for d, subjects in sorted(days.items())
    ]}


async def fetch_schedule(study_group_id: str, start: date, end: date) -> dict:
    """Повний цикл: запит до сервера -> перевірка -> перетворення у формат бота."""
    rows = await fetch_rows(study_group_id, start, end)
    return rows_to_schedule(rows)