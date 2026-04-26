import os
import asyncio
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
from ai_utils import parse_schedule
from database import save_lessons, init_db

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

app = Client(
    "schedule bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    await message.reply_text(
        f"Привіт, {message.from_user.first_name}! 👋\n\n"
        "Я твій розумний помічник з розкладом. "
        "Скоро ти зможеш скидати мені скріншоти або текст уроків, "
        "а я буду нагадувати про ДЗ та дедлайни!"
    )

@app.on_message(filters.text & filters.private)
async def echo_handler(client: Client, message: Message):
        await message.reply_text(f"Ти скинув повідомлення!")

@app.on_message(filters.photo & filters.private)
async def echo_handler(client: Client, message: Message):
    await message.reply(f"Ти скинув фото!")
    file_path = await message.download()
    result = await parse_schedule(file_path)
    if result:
        status_msg = await message.reply_text('Бот розпізнав ваш розклад! Записую його в базу данних.')
        save_lessons(result)
        print('Дані в бд є')
        await status_msg.edit_text('Бот успішно записав ваш розклад в базу данних!')
    elif result is None:
        await message.reply_text('Бот не розпізнав ваш розклад!')


if __name__ == "__main__":
    init_db()
    print("--- БОТ ЗАПУЩЕНИЙ ---")
    app.run()