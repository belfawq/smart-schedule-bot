"""Форматування розкладу для повідомлень Telegram (parse_mode=HTML)."""
import html
from datetime import date, timedelta

def escape(text: str) -> str:
    """Екранування для parse_mode=HTML (апостроф залишаємо як є)."""
    return html.escape(text, quote=False)


DAY_NAMES_UA = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
LESSON_TYPES_UA = {"lecture": "Лекція", "practice": "Практика", "lab": "Лабораторна"}


def format_day_title(d: date) -> str:
    return f"{DAY_NAMES_UA[d.weekday()]}, {d.strftime('%d.%m')}"


def format_audience(audience):
    """«29» -> «ауд. 29»; «ауд. 29, корп. 4» і «онлайн» залишаються як є."""
    if not audience:
        return None
    return f"ауд. {audience}" if audience[0].isdigit() else audience


def lesson_details(l, with_teacher: bool) -> str:
    """Рядок деталей заняття: тип · підгрупа · аудиторія [· викладач]."""
    # l — рядок таблиці lessons або словник заняття з розпізнаного розкладу
    get = (lambda k: l[k] if k in l.keys() else None)
    parts = [
        LESSON_TYPES_UA.get(get("type"), get("type")) if get("type") else None,
        f"{get('subgroup')} підгрупа" if get("subgroup") else None,
        format_audience(get("audience")),
        get("teacher") if with_teacher else None,
    ]
    return escape(" · ".join(p for p in parts if p))


def format_lesson(l, with_teacher: bool) -> str:
    time = l["start_time"] + (f"–{l['end_time']}" if l["end_time"] else "")
    text = f"🕐 <b>{time}</b>\n<b>{escape(l['subject'])}</b>"
    details = lesson_details(l, with_teacher)
    if details:
        text += f"\n<i>{details}</i>"
    return text


def format_lessons(d: date, lessons) -> str:
    title = f"📅 <b>{format_day_title(d)}</b>"
    if not lessons:
        return f"{title}\n\nПар немає 🎉"
    return title + "\n\n" + "\n\n".join(format_lesson(l, with_teacher=True) for l in lessons)


def week_bounds(today: date) -> tuple[date, date]:
    """
    Понеділок і неділя тижня, який треба показати.
    У будні — поточний тиждень, у суботу й неділю — наступний,
    бо поточний навчальний тиждень уже завершився.
    """
    monday = today - timedelta(days=today.weekday())
    if today.weekday() >= 5:  # 5 — субота, 6 — неділя
        monday += timedelta(days=7)
    return monday, monday + timedelta(days=6)


def format_week(monday: date, sunday: date, lessons) -> str:
    by_day = {}
    for l in lessons:
        by_day.setdefault(l["lesson_date"], []).append(l)

    blocks = [f"🗓 <b>Розклад на тиждень {monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}</b>"]
    for i in range(7):
        d = monday + timedelta(days=i)
        day_lessons = by_day.get(d.isoformat(), [])
        if not day_lessons and d.weekday() >= 5:
            continue  # вихідні без пар не показуємо
        lines = [f"📌 <b>{format_day_title(d)}</b>"]
        if not day_lessons:
            lines.append("Пар немає 🎉")
        for l in day_lessons:
            lines.append(f"<code>{l['start_time']}</code> {escape(l['subject'])}")
            details = lesson_details(l, with_teacher=False)
            if details:
                lines.append(f"└ <i>{details}</i>")
        blocks.append("\n".join(lines))
    blocks.append("<i>Подробиці з викладачами: /today, /tomorrow</i>")
    return "\n\n".join(blocks)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Ділить довгий текст на частини за межами днів (ліміт Telegram — 4096 символів)."""
    parts, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    parts.append(current)
    return parts


def format_schedule_preview(data: dict) -> str:
    """Попередній перегляд розпізнаного розкладу перед збереженням."""
    lines = ["🔍 <b>Ось що я розпізнав.</b> Перевір, будь ласка:"]
    for day in data["schedule"]:
        d = date.fromisoformat(day["date"])
        lines.append(f"\n📌 <b>{format_day_title(d)}</b>")
        if not day["subjects"]:
            lines.append("Пар немає")
        for sub in sorted(day["subjects"], key=lambda x: x.get("time") or ""):
            time = (sub.get("time") or "??:??") + (f"–{sub['end_time']}" if sub.get("end_time") else "")
            lines.append(f"<code>{time}</code> {escape(sub.get('name') or '?')}")
            details = lesson_details(sub, with_teacher=True)
            if details:
                lines.append(f"└ <i>{details}</i>")
    return "\n".join(lines)