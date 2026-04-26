import google.generativeai
import google.generativeai as genai
import os
import json
from dotenv import load_dotenv
load_dotenv()

AI_API_KEY = os.getenv("AI_API_KEY")
genai.configure(api_key=AI_API_KEY)
model = google.generativeai.GenerativeModel('models/gemini-2.5-flash', system_instruction="""
                                              Ти - помічник для того щоб витягувати розклад з зображення.
                                                                                   Твоя задача: перетворити побачене ТІЛЬКИ у JSON формат по такій структурі:
                                                                                   { "schedule": [
                                                                                   {
                                                                                       "day": "Monday", "subjects": [
                                                                                           "name": "Назва:", 
                                                                                           "time": "09:00", 
                                                                                           "type": "lecture", 
                                                                                           "teacher": "Фамілія та ініціали",
                                                                                           "audience": "Номер аудиторії"}] } ] }
                                                                                   ] 
                                                                                   Якщо на зображенні не видно імені викладача, - повертаєш null (без лапок), якщо на зображенні взагалі немає розкладу повертаєш null, 
                                                                                   якщо номер аудиторії не вказано — повертай null
                                                                                     Жодного іншого тексту, крім JSON не пиши) """)

async def parse_schedule(file_path):
    uploaded_file = genai.upload_file(path=file_path)
    response = await model.generate_content_async([uploaded_file, "Оброби цей розклад"])
    data = json.loads(response.text.replace("```json", "").replace("```", "").strip())
    os.remove(file_path)
    return data
