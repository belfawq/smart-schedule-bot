"""
Прив'язка академічної групи до розкладу в АСУ «ВНЗ».

Адміністратор чату групи покроково вибирає кнопками те саме, що й у віджеті
розкладу на сайті: факультет -> форма навчання -> курс -> група.
Бот зберігає ідентифікатор групи (aStudyGroupID) і одразу завантажує розклад.
"""
import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from access import is_chat_admin
from parser import ScheduleSourceError, fetch_filters, fetch_study_groups
from schedule_sync import sync_group

router = Router()
GROUP_CHATS = {ChatType.GROUP, ChatType.SUPERGROUP}
GROUPS_PAGE = 20      # груп на одній сторінці
FILTERS_TTL = 600     # скільки секунд зберігати довідник факультетів у пам'яті

_filters_cache = {"data": None, "time": 0.0}
_group_names: dict[str, str] = {}  # id групи в АСУ «ВНЗ» -> назва (з останнього списку)


async def get_filters() -> dict:
    if _filters_cache["data"] is None or time.time() - _filters_cache["time"] > FILTERS_TTL:
        _filters_cache["data"] = await fetch_filters()
        _filters_cache["time"] = time.time()
    return _filters_cache["data"]


def name_of(pairs, key) -> str:
    return dict(pairs).get(key, key)


def rows(count: int, width: int) -> list[int]:
    """Розміри рядків клавіатури: count кнопок по width у рядку."""
    return [width] * (count // width) + ([count % width] if count % width else [])


def nav_buttons(kb: InlineKeyboardBuilder):
    kb.button(text="↩️ Почати спочатку", callback_data="ss_r")
    kb.button(text="❌ Пізніше", callback_data="ss_x")


async def send_join_button(bot: Bot, chat_id: int, group_id: int, text: str):
    """Повідомлення в чаті групи з кнопкою для підключення студентів до бота."""
    me = await bot.me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🔑 Підключитися до бота", url=f"https://t.me/{me.username}?start=join_{group_id}")]])
    await bot.send_message(chat_id, f"{text}\n\nУчасники чату: натисніть кнопку нижче, щоб "
                           "отримати доступ до розкладу і домашніх завдань в особистих "
                           "повідомленнях.", reply_markup=kb)


async def faculties_view():
    filters = await get_filters()
    kb = InlineKeyboardBuilder()
    for key, name in filters["faculties"]:
        kb.button(text=name, callback_data=f"ss_f:{key}")
    kb.button(text="❌ Пізніше", callback_data="ss_x")
    kb.adjust(*rows(len(filters["faculties"]), 2), 1)
    text = ("🔗 <b>Прив'язка до розкладу АСУ «ВНЗ»</b>\n"
            "Тоді бот сам завантажуватиме розклад групи.\n\n"
            "Крок 1/4. Вибери факультет:")
    return text, kb.as_markup()


async def start_selection(message: Message, creating: bool = False):
    """
    Надсилає перший крок вибору групи.
    creating=True — чат ще не прив'язаний до групи: група буде створена після вибору
    (команда /create_group без шифру).
    """
    try:
        text, markup = await faculties_view()
    except ScheduleSourceError as e:
        logging.warning("АСУ «ВНЗ» недоступна: %s", e)
        hint = ("Спробуй пізніше або створи групу вручну: /create_group КН-21" if creating
                else "Спробуй пізніше командою /site")
        await message.answer(f"Не вдалося отримати список факультетів з АСУ «ВНЗ» 😔\n{e}\n{hint}")
        return
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("site"), F.chat.type.in_(GROUP_CHATS))
async def site_command(message: Message, bot: Bot):
    if db.get_chat_group(message.chat.id) is None:
        await message.answer("Спершу створи групу: /create_group КН-21")
        return
    if not await is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Прив'язати розклад може лише адміністратор чату.")
        return
    await start_selection(message)


@router.callback_query(F.data.startswith("ss_"))
async def site_step(cb: CallbackQuery, bot: Bot):
    if not await is_chat_admin(bot, cb.message.chat.id, cb.from_user.id):
        return await cb.answer("Вибирати групу може лише адміністратор чату", show_alert=True)
    group_id = db.get_chat_group(cb.message.chat.id)  # None, якщо групу ще створюємо

    action, *args = cb.data.split(":")
    try:
        if action == "ss_x":
            await cb.message.edit_text("Прив'язку до АСУ «ВНЗ» відкладено. Повернутися: /site")
        elif action == "ss_r":
            text, markup = await faculties_view()
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        elif action == "ss_f":
            await show_forms(cb, *args)
        elif action == "ss_e":
            await show_courses(cb, *args)
        elif action == "ss_c":
            await show_groups(cb, *args)
        elif action == "ss_g":
            await cb.answer()
            return await select_group(cb, bot, group_id, args[0])
    except ScheduleSourceError as e:
        logging.warning("АСУ «ВНЗ» недоступна: %s", e)
        await cb.message.edit_text(f"Сервер АСУ «ВНЗ» не відповідає 😔\n{e}\n"
                                   "Спробуй пізніше командою /site")
    await cb.answer()


async def show_forms(cb: CallbackQuery, faculty: str):
    filters = await get_filters()
    kb = InlineKeyboardBuilder()
    for key, name in filters["forms"]:
        kb.button(text=name, callback_data=f"ss_e:{faculty}:{key}")
    nav_buttons(kb)
    kb.adjust(*rows(len(filters["forms"]), 2), 2)
    await cb.message.edit_text(
        f"Факультет: <b>{name_of(filters['faculties'], faculty)}</b>\n\n"
        "Крок 2/4. Вибери форму навчання:", parse_mode="HTML", reply_markup=kb.as_markup())


async def show_courses(cb: CallbackQuery, faculty: str, form: str):
    filters = await get_filters()
    kb = InlineKeyboardBuilder()
    for key, name in filters["courses"]:
        kb.button(text=name, callback_data=f"ss_c:{faculty}:{form}:{key}")
    nav_buttons(kb)
    kb.adjust(*rows(len(filters["courses"]), 3), 2)
    await cb.message.edit_text(
        f"Факультет: <b>{name_of(filters['faculties'], faculty)}</b>\n"
        f"Форма навчання: <b>{name_of(filters['forms'], form)}</b>\n\n"
        "Крок 3/4. Вибери курс:", parse_mode="HTML", reply_markup=kb.as_markup())


async def show_groups(cb: CallbackQuery, faculty: str, form: str, course: str, page: str = "0"):
    filters = await get_filters()
    groups = await fetch_study_groups(faculty, form, course)
    _group_names.update(dict(groups))
    header = (f"Факультет: <b>{name_of(filters['faculties'], faculty)}</b>\n"
              f"Форма навчання: <b>{name_of(filters['forms'], form)}</b>\n"
              f"Курс: <b>{name_of(filters['courses'], course)}</b>\n\n")
    kb = InlineKeyboardBuilder()
    if not groups:
        nav_buttons(kb)
        kb.adjust(2)
        return await cb.message.edit_text(header + "Груп не знайдено 🤷 Спробуй інший курс.",
                                          parse_mode="HTML", reply_markup=kb.as_markup())

    pages = (len(groups) + GROUPS_PAGE - 1) // GROUPS_PAGE
    page = max(0, min(int(page), pages - 1))
    chunk = groups[page * GROUPS_PAGE:(page + 1) * GROUPS_PAGE]
    for key, name in chunk:
        kb.button(text=name, callback_data=f"ss_g:{key}")
    sizes = rows(len(chunk), 2)
    if pages > 1:
        base = f"ss_c:{faculty}:{form}:{course}"
        if page > 0:
            kb.button(text="◀️", callback_data=f"{base}:{page - 1}")
        if page < pages - 1:
            kb.button(text="▶️", callback_data=f"{base}:{page + 1}")
        sizes.append(int(page > 0) + int(page < pages - 1))
    nav_buttons(kb)
    sizes.append(2)
    kb.adjust(*sizes)
    title = "Крок 4/4. Вибери свою групу"
    if pages > 1:
        title += f" (сторінка {page + 1}/{pages})"
    await cb.message.edit_text(header + title + ":", parse_mode="HTML",
                               reply_markup=kb.as_markup())


async def select_group(cb: CallbackQuery, bot: Bot, group_id, site_group_id: str):
    name = _group_names.get(site_group_id)
    shown = f"«{name}»" if name else site_group_id
    chat_id = cb.message.chat.id
    created = False

    if group_id is None:
        # Створення групи: шифр — назва групи в АСУ «ВНЗ»
        if not name:
            return await cb.message.edit_text(
                "Не вдалося визначити назву групи (бот перезапускався). Почни знову: /create_group")
        existing = db.get_group_by_code(name)
        if existing and db.get_group_chats(existing["id"]):
            return await cb.message.edit_text(
                f"Група {existing['code']} уже прив'язана до іншого чату.")
        group_id = existing["id"] if existing else db.create_group(name)
        db.bind_chat(chat_id, group_id)
        created = True

    db.set_site_group_id(group_id, site_group_id, name)
    await cb.message.edit_text(f"Групу {shown} вибрано ✅\nЗавантажую розклад... ⏳")
    try:
        count, start, end = await sync_group(group_id)
        result = (f"✅ Групу прив'язано до {shown} в АСУ «ВНЗ».\n"
                  f"Завантажено занять: {count} "
                  f"(з {start.strftime('%d.%m')} по {end.strftime('%d.%m')}).\n\n"
                  "Розклад: /today, /tomorrow, /week")
    except ScheduleSourceError as e:
        result = (f"Групу {shown} прив'язано ✅, але розклад завантажити не вдалося 😔\n{e}\n"
                  "Спробуй пізніше: /update")
    await cb.message.edit_text(result)

    if created:
        await send_join_button(bot, chat_id, group_id,
                               f"Групу {db.get_group(group_id)['code']} створено ✅")