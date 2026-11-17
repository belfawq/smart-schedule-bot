import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from dotenv import load_dotenv

from ai_utils import parse_schedule
from database import bind_chat, get_user_group, init_db, save_lessons, set_user_group

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Роутер для особистих повідомлень і роутер для групових чатів
private_router = Router()
private_router.message.filter(F.chat.type == ChatType.PRIVATE)

group_router = Router()
group_router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


# Скінченний автомат станів (FSM): бот чекає на розклад після команди /add
class UploadSchedule(StatesGroup):
    waiting = State()


# Буфер для альбомів: Telegram надсилає кожне фото альбому окремим повідомленням
# з однаковим media_group_id, тому збираємо їх і обробляємо разом
album_buffer: dict[str, list[Message]] = {}
ALBUM_WAIT_SECONDS = 1.5


# ---------- Особисті повідомлення ----------

@private_router.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        f"Привіт, {message.from_user.first_name}! 👋\n\n"
        "Я твій помічник з розкладом і домашніми завданнями.\n\n"
        "1️⃣ Вкажи свою групу: /group КН-21\n"
        "2️⃣ Завантаж розклад: /add, а потім фото або текст розкладу"
    )


@private_router.message(Command("group"))
async def group_handler(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Вкажи шифр групи, наприклад: /group КН-21")
        return
    group_code = command.args.split()[0].upper()
    set_user_group(message.from_user.id, message.from_user.first_name,
                   message.from_user.username, group_code)
    await message.answer(f"Тебе додано до групи {group_code} ✅")


@private_router.message(Command("add"))
async def add_handler(message: Message, state: FSMContext):
    if get_user_group(message.from_user.id) is None:
        await message.answer("Спершу вкажи свою групу: /group КН-21")
        return
    await state.set_state(UploadSchedule.waiting)
    await message.answer("Надішли розклад у вигляді фото або тексту 📅")


async def process_schedule(message: Message, state: FSMContext, data, is_image: bool):
    """Спільна логіка для тексту та фото: розпізнати розклад і зберегти для групи."""
    group_id = get_user_group(message.from_user.id)
    status_msg = await message.answer("Розпізнаю розклад... ⏳")
    try:
        result = await parse_schedule(data, is_image=is_image)
    except Exception as e:
        logging.exception("Помилка розпізнавання: %s", e)
        result = None

    if result:
        save_lessons(result, group_id, source="ai")
        await status_msg.edit_text("Розклад успішно збережено для твоєї групи ✅")
    else:
        await status_msg.edit_text("Не вдалося розпізнати розклад 😔 Спробуй інше фото або текст.")
    await state.clear()


@private_router.message(UploadSchedule.waiting, F.photo)
async def photo_handler(message: Message, state: FSMContext, bot: Bot):
    if message.media_group_id:
        if message.media_group_id in album_buffer:
            album_buffer[message.media_group_id].append(message)
            return
        # Перше фото альбому: чекаємо, поки надійдуть решта, і обробляємо все разом
        album_buffer[message.media_group_id] = [message]
        await asyncio.sleep(ALBUM_WAIT_SECONDS)
        messages = album_buffer.pop(message.media_group_id)
    else:
        messages = [message]

    images = []
    for m in messages:
        file = await bot.download(m.photo[-1])  # photo[-1] — найбільша якість
        images.append(file.read())
    await process_schedule(message, state, images, is_image=True)


@private_router.message(UploadSchedule.waiting, F.text & ~F.text.startswith("/"))
async def text_handler(message: Message, state: FSMContext):
    await process_schedule(message, state, message.text, is_image=False)


@private_router.message(StateFilter(None), F.photo | (F.text & ~F.text.startswith("/")))
async def no_state_handler(message: Message):
    # Для альбому відповідаємо лише один раз
    if message.media_group_id:
        if message.media_group_id in album_buffer:
            return
        album_buffer[message.media_group_id] = []
        asyncio.get_running_loop().call_later(
            ALBUM_WAIT_SECONDS, album_buffer.pop, message.media_group_id, None
        )
    await message.answer("Щоб завантажити розклад, спершу напиши /add")


# ---------- Групові чати ----------

@group_router.message(Command("bind"))
async def bind_handler(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Вкажи шифр групи, наприклад: /bind КН-21")
        return
    group_code = command.args.split()[0].upper()
    bind_chat(message.chat.id, group_code)
    await message.answer(f"Чат прив'язано до групи {group_code} ✅")


# ---------- Запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_routers(private_router, group_router)

    print("--- БОТ ЗАПУЩЕНИЙ ---")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())