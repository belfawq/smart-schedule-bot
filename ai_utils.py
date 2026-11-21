import os
from datetime import date, timedelta
from typing import Optional

from dotenv import load_dotenv

import clock
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

AI_API_KEY = os.getenv("AI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# api_version="v1beta": у стабільній v1 системні інструкції (system_instruction)
# і схема відповіді для Gemini API поки не підтримуються
client = genai.Client(
    api_key=AI_API_KEY,
    http_options=types.HttpOptions(api_version="v1beta"),
)


# ---------- Схема відповіді (structured output) ----------
# Gemini отримує цю схему і зобов'язаний повернути JSON саме такої структури,
# тому не потрібно вирізати ```json і вручну перевіряти формат.

class Subject(BaseModel):
    name: str = Field(description="Назва предмета")
    time: str = Field(description="Час початку пари у форматі HH:MM")
    end_time: Optional[str] = Field(None, description="Час закінчення пари у форматі HH:MM")
    type: Optional[str] = Field(None, description="lecture, practice, lab або null")
    teacher: Optional[str] = Field(None, description="Прізвище та ініціали викладача")
    audience: Optional[str] = Field(None, description="Номер аудиторії")
    subgroup: Optional[str] = Field(None, description="Номер підгрупи, якщо заняття лише для підгрупи")


class Day(BaseModel):
    day: str = Field(description="День тижня англійською: Monday ... Sunday")
    date: Optional[str] = Field(None, description="Дата цього дня у форматі YYYY-MM-DD")
    subjects: list[Subject]


class Schedule(BaseModel):
    schedule: list[Day]


SYSTEM_PROMPT = """
Ти - помічник, який витягує розклад занять із зображень або тексту.
Тобі можуть надіслати одне або кілька зображень - це частини одного розкладу.
Об'єднай дані з усіх зображень в один розклад. Якщо той самий день є на кількох
зображеннях, запиши його один раз і не дублюй однакові пари.
Для кожного дня вкажи його дату у форматі YYYY-MM-DD. Якщо в розкладі не вказано рік,
визнач його за сьогоднішньою датою. Якщо дати немає зовсім - став null.
Якщо якесь поле не вказане - став null.
Якщо в даних немає розкладу - поверни порожній список schedule.
"""

CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    response_mime_type="application/json",
    response_schema=Schedule,
    temperature=0,  # мінімум "творчості" - потрібне точне розпізнавання
)


async def parse_schedule(data, is_image: bool):
    """
    data: текст розкладу (str) або список зображень у байтах (list[bytes]).
    Повертає словник {"schedule": [...]} або None, якщо розклад не знайдено.
    """
    today = clock.today()
    today_note = f"Сьогодні {today.isoformat()} ({DAY_NAMES[today.weekday()]})."

    if is_image:
        images = data if isinstance(data, list) else [data]
        contents = [types.Part.from_bytes(data=img, mime_type="image/jpeg") for img in images]
        contents.append(f"{today_note} Оброби цей розклад")
    else:
        contents = [f"{today_note} Оброби цей розклад: {data}"]

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=CONFIG,
    )

    schedule = response.parsed
    if schedule is None and response.text:
        schedule = Schedule.model_validate_json(response.text)
    if schedule is None or not schedule.schedule:
        return None
    return fill_missing_dates(schedule.model_dump(), today)


DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def fill_missing_dates(data, today: date):
    """
    Якщо в розкладі вказано лише день тижня без дати, підставляє найближчу
    таку дату починаючи від сьогодні (розклад публікують наперед).
    Дні без дати, яку неможливо визначити, відкидаються.
    """
    result = []
    for day in data["schedule"]:
        if day.get("date"):
            try:
                day["date"] = date.fromisoformat(day["date"]).isoformat()
            except ValueError:
                day["date"] = None
        if not day.get("date"):
            name = str(day.get("day", "")).strip().capitalize()
            if name not in DAY_NAMES:
                continue
            delta = (DAY_NAMES.index(name) - today.weekday()) % 7
            day["date"] = (today + timedelta(days=delta)).isoformat()
        result.append(day)
    return {"schedule": result} if result else None