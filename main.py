import asyncio
import html
import logging
import os
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, Message
from dotenv import load_dotenv

from ai_utils import parse_schedule
from database import (bind_chat, create_group, get_chat_group, get_group, get_group_by_code,
                      get_group_chats, get_lessons, get_lessons_range, get_user_group,
                      init_db, join_group, replace_lessons_range, save_lessons,
                      set_site_group_id)
from access import is_chat_admin, is_chat_member, is_group_admin
from parser import ScheduleSourceError
from schedule_sync import sync_group
import homework
import site_link

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Роутер для особистих повідомлень і роутер для групових чатів
private_router = Router()
private_router.message.filter(F.chat.type == ChatType.PRIVATE)

group_router = Router()
group_router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

# Роутер для команд, які працюють і в особистих повідомленнях, і в групі
common_router = Router()


# Скінченний автомат станів (FSM): бот чекає на розклад після команди /add
class UploadSchedule(StatesGroup):
    waiting = State()


# Буфер для альбомів: Telegram надсилає кожне фото альбому окремим повідомленням
# з однаковим media_group_id, тому збираємо їх і обробляємо разом
album_buffer: dict[str, list[Message]] = {}
ALBUM_WAIT_SECONDS = 1.5


# ---------- Особисті повідомлення ----------

HELP_TEXT = (
    "📅 /today — пари на сьогодні\n"
    "📅 /tomorrow — пари на завтра\n"
    "🗓 /week — розклад на тиждень\n"
    "📝 /hw — додати домашнє завдання\n"
    "📋 /hw_list — домашні завдання на 2 тижні\n"
    "🔄 /update — оновити розклад із сервера АСУ «ВНЗ»"
)


async def try_join(message: Message, bot: Bot, group):
    """Додає користувача до групи, якщо він учасник чату цієї групи в Telegram."""
    chats = get_group_chats(group["id"])
    for chat_id in chats:
        if await is_chat_member(bot, chat_id, message.from_user.id):
            join_group(message.from_user.id, message.from_user.first_name,
                       message.from_user.username, group["id"])
            await message.answer(f"Тебе додано до групи {group['code']} ✅\n\n{HELP_TEXT}")
            return
    await message.answer(
        f"Ти не учасник чату групи {group['code']} 🤔\n"
        "Доступ до розкладу мають лише учасники чату групи. Попроси старосту додати тебе в чат."
    )


@private_router.message(CommandStart())
async def start_handler(message: Message, command: CommandObject, bot: Bot):
    # Перехід за кнопкою з чату групи: t.me/<бот>?start=join_<id групи>
    if command.args and command.args.startswith("join_"):
        group = get_group(int(command.args.removeprefix("join_"))) \
            if command.args.removeprefix("join_").isdigit() else None
        if group:
            await try_join(message, bot, group)
            return

    group_id = get_user_group(message.from_user.id)
    if group_id is not None:
        await message.answer(f"Привіт, {message.from_user.first_name}! 👋\n"
                             f"Твоя група: {get_group(group_id)['code']}\n\n{HELP_TEXT}")
        return
    await message.answer(
        f"Привіт, {message.from_user.first_name}! 👋\n\n"
        "Я твій помічник з розкладом і домашніми завданнями.\n\n"
        "Як почати:\n"
        "1️⃣ Староста додає мене в чат групи, пише там /create_group і вибирає групу кнопками\n"
        "2️⃣ Ти натискаєш кнопку під моїм повідомленням у чаті групи "
        "або пишеш тут /group КН-21\n\n"
        "Я перевірю, що ти є в чаті групи, і відкрию доступ."
    )


@private_router.message(Command("group"))
async def group_handler(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        await message.answer("Вкажи шифр групи, наприклад: /group КН-21")
        return
    group = get_group_by_code(command.args)
    if group is None:
        await message.answer(
            f"Групу {command.args.strip().upper()} ще не створено.\n"
            "Попроси старосту додати мене в чат групи і написати там /create_group "
            f"{command.args.strip().upper()}")
        return
    await try_join(message, bot, group)


@private_router.message(Command("add"))
async def add_handler(message: Message, state: FSMContext, bot: Bot):
    group_id = get_user_group(message.from_user.id)
    if group_id is None:
        await message.answer("Спершу вкажи свою групу: /group КН-21")
        return
    # Розклад спільний для всієї групи, тому змінювати його можуть лише адміністратори чату
    if not await is_group_admin(bot, group_id, message.from_user.id):
        await message.answer("Завантажувати розклад можуть лише адміністратори чату групи "
                             "(староста та заступники).")
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
        dates = save_lessons(result, group_id, source="ai")
        days_list = "\n".join(format_day_title(date.fromisoformat(d)) for d in dates)
        await status_msg.edit_text(f"Розклад збережено для твоєї групи ✅\n\n{days_list}")
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

@group_router.message(Command("create_group"))
async def create_group_handler(message: Message, command: CommandObject, bot: Bot):
    if not await is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Створити групу може лише адміністратор чату.")
        return

    group_id = get_chat_group(message.chat.id)
    if group_id is not None:
        await site_link.send_join_button(
            bot, message.chat.id, group_id,
            f"Цей чат уже прив'язано до групи {get_group(group_id)['code']} ✅")
        if not get_group(group_id)["site_group_id"]:
            await site_link.start_selection(message)
        return

    # Без шифру: група вибирається кнопками з АСУ «ВНЗ», шифр береться звідти
    if not command.args:
        await site_link.start_selection(message, creating=True)
        return

    # Запасний варіант, коли АСУ «ВНЗ» недоступна: шифр вручну
    group = get_group_by_code(command.args)
    if group and get_group_chats(group["id"]):
        await message.answer(f"Група {group['code']} уже прив'язана до іншого чату.")
        return
    group_id = group["id"] if group else create_group(command.args)
    bind_chat(message.chat.id, group_id)
    await site_link.send_join_button(
        bot, message.chat.id, group_id,
        f"Групу {get_group(group_id)['code']} створено і прив'язано до цього чату ✅")


# ---------- Перегляд розкладу (особисті повідомлення і група) ----------

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
    parts = [
        LESSON_TYPES_UA.get(l["type"], l["type"]) if l["type"] else None,
        f"{l['subgroup']} підгрупа" if l["subgroup"] else None,
        format_audience(l["audience"]),
        l["teacher"] if with_teacher else None,
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


def resolve_group(message: Message):
    """В особистих — група користувача, у груповому чаті — група, прив'язана до чату."""
    if message.chat.type == ChatType.PRIVATE:
        return get_user_group(message.from_user.id)
    return get_chat_group(message.chat.id)


async def send_day_schedule(message: Message, d: date):
    group_id = resolve_group(message)
    if group_id is None:
        hint = "/group КН-21" if message.chat.type == ChatType.PRIVATE else "/create_group КН-21"
        await message.answer(f"Спершу вкажи групу: {hint}")
        return
    await message.answer(format_lessons(d, get_lessons(group_id, d)), parse_mode="HTML")


@common_router.message(Command("today"))
async def today_handler(message: Message):
    await send_day_schedule(message, date.today())


@common_router.message(Command("tomorrow"))
async def tomorrow_handler(message: Message):
    await send_day_schedule(message, date.today() + timedelta(days=1))


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


@common_router.message(Command("week"))
async def week_handler(message: Message):
    group_id = resolve_group(message)
    if group_id is None:
        hint = "/group КН-21" if message.chat.type == ChatType.PRIVATE else "/create_group КН-21"
        await message.answer(f"Спершу вкажи групу: {hint}")
        return
    monday, sunday = week_bounds(date.today())
    lessons = get_lessons_range(group_id, monday, sunday)
    if not lessons:
        await message.answer(
            f"Розкладу на тиждень {monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')} "
            "ще немає 🤷\nОнови його: /update"
        )
        return
    for part in split_message(format_week(monday, sunday, lessons)):
        await message.answer(part, parse_mode="HTML")


# ---------- Вебпарсинг розкладу (АСУ «ВНЗ») ----------

@common_router.message(Command("site_id"))
async def site_id_handler(message: Message, command: CommandObject, bot: Bot):
    group_id = resolve_group(message)
    if group_id is None:
        await message.answer("Спершу вкажи групу: /group КН-21 (або /create_group у чаті групи)")
        return
    if not await is_group_admin(bot, group_id, message.from_user.id):
        await message.answer("Змінити ID групи можуть лише адміністратори чату групи.")
        return
    if not command.args:
        await message.answer("Вкажи ID групи в АСУ «ВНЗ» (aStudyGroupID): /site_id ABC123")
        return
    set_site_group_id(group_id, command.args.strip())
    await message.answer("ID групи збережено ✅ Тепер можна оновити розклад: /update")


@common_router.message(Command("update"))
async def update_handler(message: Message):
    group_id = resolve_group(message)
    if group_id is None:
        await message.answer("Спершу вкажи групу: /group КН-21")
        return
    status_msg = await message.answer("Завантажую розклад з АСУ «ВНЗ»... ⏳")
    try:
        count, start, end = await sync_group(group_id)
    except ScheduleSourceError as e:
        logging.warning("Помилка вебпарсингу: %s", e)
        await status_msg.edit_text(
            f"Не вдалося оновити розклад 😔\n{e}\n\n"
            "Збережений раніше розклад залишився доступним: /today, /tomorrow"
        )
        return
    await status_msg.edit_text(
        f"Розклад оновлено ✅\nЗанять: {count} "
        f"(з {start.strftime('%d.%m')} по {end.strftime('%d.%m')})"
    )


# ---------- Запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_routers(homework.router, site_link.router, common_router, private_router,
                       group_router)

    # Меню команд, яке Telegram показує біля поля вводу
    await bot.set_my_commands([
        BotCommand(command="today", description="Пари на сьогодні"),
        BotCommand(command="tomorrow", description="Пари на завтра"),
        BotCommand(command="week", description="Розклад на тиждень"),
        BotCommand(command="hw", description="Додати домашнє завдання"),
        BotCommand(command="hw_list", description="Домашні завдання на 2 тижні"),
        BotCommand(command="update", description="Оновити розклад з АСУ «ВНЗ»"),
        BotCommand(command="group", description="Вказати свою групу"),
        BotCommand(command="add", description="Завантажити розклад (староста)"),
    ])

    print("--- БОТ ЗАПУЩЕНИЙ ---")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())