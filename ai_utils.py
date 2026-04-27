import google.generativeai
import google.generativeai as genai
import os
import json
from dotenv import load_dotenv
load_dotenv()

AI_API_KEY = os.getenv("AI_API_KEY")
genai.configure(api_key=AI_API_KEY)
model = google.generativeai.GenerativeModel('models/gemini-2.5-flash', system_instruction="""
                                              Ти - помічник для того щоб витягувати розклад з зображення та відформатовувати текст, і з нього також доставати тільки розклад.
                                                                                   Твоя задача: перетворити побачене ТІЛЬКИ у JSON формат по такій структурі:
                                                                                   { "schedule": [
                                                                                   {
                                                                                       "day": "Monday", "subjects": [
                                                                                            {"name": "Назва:", 
                                                                                           "time": "09:00", 
                                                                                           "type": "lecture", 
                                                                                           "teacher": "Фамілія та ініціали",
                                                                                           "audience": "Номер аудиторії"}] } ] }
                                                                                   ] 
                                                                                   Якщо на зображенні або в тексті не видно імені викладача, - повертаєш null (без лапок), якщо на зображенні або в тексті взагалі немає розкладу повертаєш null, 
                                                                                   якщо номер аудиторії не вказано — повертай null.
                                                                                    Не додавай жодних пояснень, привітань чи Markdown-розмітки
                                                                                     Жодного іншого тексту, крім JSON не пиши) """)

async def parse_schedule(data, is_image):
    if is_image:
        uploaded_file = genai.upload_file(path=data)
        response = await model.generate_content_async([uploaded_file, "Оброби цей розклад"])
        os.remove(data)
    else:
        response = await model.generate_content_async([f"Оброби цей розклад..{data}"])
    data = json.loads(response.text.replace("```json", "").replace("```", "").strip())
    return data
