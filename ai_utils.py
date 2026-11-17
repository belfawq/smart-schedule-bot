import os
from typing import Optional

from dotenv import load_dotenv
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
    week_type: Optional[str] = Field(
        None, description="odd - чисельник, even - знаменник, null - щотижня"
    )


class Day(BaseModel):
    day: str = Field(description="День тижня англійською: Monday ... Sunday")
    subjects: list[Subject]


class Schedule(BaseModel):
    schedule: list[Day]


SYSTEM_PROMPT = """
Ти - помічник, який витягує розклад занять із зображень або тексту.
Тобі можуть надіслати одне або кілька зображень - це частини одного розкладу.
Об'єднай дані з усіх зображень в один розклад. Якщо той самий день є на кількох
зображеннях, запиши його один раз і не дублюй однакові пари.
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
    if is_image:
        images = data if isinstance(data, list) else [data]
        contents = [types.Part.from_bytes(data=img, mime_type="image/jpeg") for img in images]
        contents.append("Оброби цей розклад")
    else:
        contents = [f"Оброби цей розклад: {data}"]

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
    return schedule.model_dump()