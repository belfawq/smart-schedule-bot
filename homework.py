"""
Облік домашніх завдань.

Студент додає завдання в особистих повідомленнях:
  /hw -> «для себе» або «для групи» -> предмет -> текст і вкладення -> дедлайн.
Особисте завдання бачить лише автор. Групове надсилається в чат групи.
Якщо на цю дату з цього предмета вже є групове завдання, заміну
підтверджує будь-хто з адміністраторів чату (зазвичай староста або його заступники).

Файли не завантажуються на сервер бота: зберігаються лише їхні file_id,
за якими Telegram дозволяє надіслати той самий файл повторно.
"""
import html
import logging
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InputMediaDocument, InputMediaPhoto,
                           InputMediaVideo, Message, User)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from access import is_chat_admin

router = Router()

DAY_NAMES_UA = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]


class HomeworkForm(StatesGroup):
    scope = State()           # для себе / для групи
    subject = State()         # вибір предмета кнопкою
    custom_subject = State()  # введення назви предмета вручну
    content = State()         # текст і вкладення
    deadline = State()        # вибір дедлайну
    custom_date = State()     # введення дати вручну


# Чернетки завдань: user_id -> дані. Зберігаються в пам'яті, бо фото з альбому
# приходять окремими повідомленнями майже одночасно і мають додаватися в одну чернетку
drafts: dict[int, dict] = {}


# ---------- Допоміжні функції ----------

def esc(text) -> str:
    return html.escape(str(text), quote=False)


def fmt_date(d: date) -> str:
    return f"{DAY_NAMES_UA[d.weekday()]}, {d.strftime('%d.%m')}"


def user_link(user: User) -> str:
    return f'<a href="tg://user?id={user.id}">{esc(user.full_name)}</a>'


def format_homework(hw, attachments_count: int = 0) -> str:
    due = date.fromisoformat(hw["due_date"])
    text = (f"📚 <b>{esc(hw['subject'])}</b> — до {due.strftime('%d.%m')} "
            f"({DAY_NAMES_UA[due.weekday()].lower()})\n{esc(hw['text'])}")
    if attachments_count:
        text += f"\n📎 Вкладень: {attachments_count}"
    return text


async def send_attachments(bot: Bot, chat_id: int, attachments, reply_to: int | None = None):
    """Надсилає вкладення за file_id: фото й відео — альбомами, документи — окремим альбомом."""
    media = [a for a in attachments if a["file_type"] in ("photo", "video")]
    docs = [a for a in attachments if a["file_type"] == "document"]
    for group, make in ((media, None), (docs, InputMediaDocument)):
        for i in range(0, len(group), 10):  # в альбомі Telegram не більше 10 елементів
            chunk = group[i:i + 10]
            if len(chunk) == 1:
                a = chunk[0]
                send = {"photo": bot.send_photo, "video": bot.send_video,
                        "document": bot.send_document}[a["file_type"]]
                await send(chat_id, a["file_id"], reply_to_message_id=reply_to)
                continue
            items = []
            for a in chunk:
                cls = make or (InputMediaPhoto if a["file_type"] == "photo" else InputMediaVideo)
                items.append(cls(media=a["file_id"]))
            await bot.send_media_group(chat_id, items, reply_to_message_id=reply_to)


async def send_homework(bot: Bot, chat_id: int, hw, header: str = "", reply_markup=None):
    """Надсилає завдання (текст і вкладення) в чат. Використовується і для нагадувань."""
    attachments = db.get_attachments(hw["id"])
    text = (header + "\n\n" if header else "") + format_homework(hw)
    msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
    if attachments:
        await send_attachments(bot, chat_id, attachments, reply_to=msg.message_id)
    return msg


def cancel_button(kb: InlineKeyboardBuilder):
    kb.button(text="❌ Скасувати", callback_data="hw_cancel")


async def lost_draft(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Чернетку втрачено (бот перезапускався). Почни знову: /hw", show_alert=True)


# ---------- Крок 1: для себе чи для групи ----------

@router.message(Command("hw"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def hw_in_group(message: Message, bot: Bot):
    """У чаті групи завдання не додаються (щоб не засмічувати чат) — ведемо в особисті."""
    me = await bot.me()
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Додати ДЗ в особистих", url=f"https://t.me/{me.username}?start=hw")
    await message.reply("Домашні завдання додаються в особистих повідомленнях з ботом 👇",
                        reply_markup=kb.as_markup())


# Перехід за кнопкою з чату групи: t.me/<бот>?start=hw
@router.message(CommandStart(deep_link=True, magic=F.args == "hw"), F.chat.type == ChatType.PRIVATE)
@router.message(Command("hw"), F.chat.type == ChatType.PRIVATE)
async def hw_start(message: Message, state: FSMContext):
    group_id = db.get_user_group(message.from_user.id)
    if group_id is None:
        await message.answer("Спершу вкажи свою групу: /group КН-21")
        return
    drafts[message.from_user.id] = {"group_id": group_id, "text": [], "attachments": [],
                                    "albums": set()}
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Для себе", callback_data="hw_scope:me")
    kb.button(text="👥 Для групи", callback_data="hw_scope:group")
    cancel_button(kb)
    kb.adjust(2, 1)
    await state.set_state(HomeworkForm.scope)
    await message.answer("Для кого це домашнє завдання?", reply_markup=kb.as_markup())


@router.callback_query(F.data == "hw_cancel")
async def hw_cancel(cb: CallbackQuery, state: FSMContext):
    drafts.pop(cb.from_user.id, None)
    await state.clear()
    await cb.message.edit_text("Скасовано.")
    await cb.answer()


@router.message(Command("cancel"), StateFilter(HomeworkForm))
async def hw_cancel_cmd(message: Message, state: FSMContext):
    drafts.pop(message.from_user.id, None)
    await state.clear()
    await message.answer("Скасовано.")


# ---------- Крок 2: предмет ----------

@router.callback_query(HomeworkForm.scope, F.data.startswith("hw_scope:"))
async def hw_scope(cb: CallbackQuery, state: FSMContext):
    draft = drafts.get(cb.from_user.id)
    if not draft:
        return await lost_draft(cb, state)
    draft["scope"] = cb.data.split(":")[1]

    today = date.today()
    subjects = db.get_group_subjects(draft["group_id"], today - timedelta(days=14),
                                     today + timedelta(days=30))
    draft["subjects"] = subjects
    if not subjects:
        await state.set_state(HomeworkForm.custom_subject)
        await cb.message.edit_text("У розкладі немає предметів. Напиши назву предмета:")
        return await cb.answer()

    kb = InlineKeyboardBuilder()
    for i, subject in enumerate(subjects):
        kb.button(text=subject[:60], callback_data=f"hw_subj:{i}")
    kb.button(text="✏️ Інший предмет", callback_data="hw_subj:other")
    cancel_button(kb)
    kb.adjust(1)
    await state.set_state(HomeworkForm.subject)
    await cb.message.edit_text("З якого предмета?", reply_markup=kb.as_markup())
    await cb.answer()


async def ask_content(message: Message, state: FSMContext, subject: str, edit: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="hw_done")
    cancel_button(kb)
    text = (f"Предмет: <b>{esc(subject)}</b>\n\n"
            "Надішли текст завдання та/або фото, файли (можна кілька повідомлень).\n"
            "Коли закінчиш — натисни «✅ Готово».")
    await state.set_state(HomeworkForm.content)
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(HomeworkForm.subject, F.data.startswith("hw_subj:"))
async def hw_subject(cb: CallbackQuery, state: FSMContext):
    draft = drafts.get(cb.from_user.id)
    if not draft:
        return await lost_draft(cb, state)
    choice = cb.data.split(":")[1]
    if choice == "other":
        await state.set_state(HomeworkForm.custom_subject)
        await cb.message.edit_text("Напиши назву предмета:")
        return await cb.answer()
    draft["subject"] = draft["subjects"][int(choice)]
    await ask_content(cb.message, state, draft["subject"], edit=True)
    await cb.answer()


@router.message(HomeworkForm.custom_subject, F.text & ~F.text.startswith("/"))
async def hw_custom_subject(message: Message, state: FSMContext):
    draft = drafts.get(message.from_user.id)
    if not draft:
        await state.clear()
        return await message.answer("Чернетку втрачено. Почни знову: /hw")
    draft["subject"] = message.text.strip()[:100]
    await ask_content(message, state, draft["subject"], edit=False)


# ---------- Крок 3: текст і вкладення ----------

@router.message(HomeworkForm.content, F.photo | F.document | F.video |
                (F.text & ~F.text.startswith("/")))
async def hw_content(message: Message, state: FSMContext):
    draft = drafts.get(message.from_user.id)
    if not draft:
        await state.clear()
        return await message.answer("Чернетку втрачено. Почни знову: /hw")

    if message.photo:
        draft["attachments"].append((message.photo[-1].file_id, "photo"))
    elif message.document:
        draft["attachments"].append((message.document.file_id, "document"))
    elif message.video:
        draft["attachments"].append((message.video.file_id, "video"))
    text = message.text or message.caption
    if text:
        draft["text"].append(text.strip())

    # На альбом відповідаємо один раз, а не на кожне фото
    if message.media_group_id:
        if message.media_group_id in draft["albums"]:
            return
        draft["albums"].add(message.media_group_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="hw_done")
    cancel_button(kb)
    await message.answer("Додано ✅ Можеш надіслати ще або натисни «✅ Готово».",
                         reply_markup=kb.as_markup())


# ---------- Крок 4: дедлайн ----------

@router.callback_query(HomeworkForm.content, F.data == "hw_done")
async def hw_done(cb: CallbackQuery, state: FSMContext):
    draft = drafts.get(cb.from_user.id)
    if not draft:
        return await lost_draft(cb, state)
    if not draft["text"] and not draft["attachments"]:
        return await cb.answer("Спершу надішли текст або файл із завданням", show_alert=True)

    next_date = db.get_next_lesson_date(draft["group_id"], draft["subject"], date.today())
    kb = InlineKeyboardBuilder()
    if next_date:
        draft["next_date"] = next_date
        d = date.fromisoformat(next_date)
        kb.button(text=f"📅 На наступну пару ({d.strftime('%d.%m')})", callback_data="hw_date:next")
    kb.button(text="🗓 Інша дата", callback_data="hw_date:other")
    cancel_button(kb)
    kb.adjust(1)
    await state.set_state(HomeworkForm.deadline)
    await cb.message.edit_text("До якої дати треба зробити?", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(HomeworkForm.deadline, F.data.startswith("hw_date:"))
async def hw_deadline(cb: CallbackQuery, state: FSMContext, bot: Bot):
    draft = drafts.get(cb.from_user.id)
    if not draft:
        return await lost_draft(cb, state)
    if cb.data == "hw_date:other":
        await state.set_state(HomeworkForm.custom_date)
        await cb.message.edit_text("Введи дату у форматі ДД.ММ (наприклад, 28.11):")
        return await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await finish(bot, cb.message, cb.from_user, state, date.fromisoformat(draft["next_date"]))


def parse_date(text: str, today: date):
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        d = datetime.strptime(f"{text}.{today.year}", "%d.%m.%Y").date()
    except ValueError:
        return None
    # «05.01», введене в грудні, — це січень наступного року
    return d if d >= today else d.replace(year=today.year + 1)


@router.message(HomeworkForm.custom_date, F.text & ~F.text.startswith("/"))
async def hw_custom_date(message: Message, state: FSMContext, bot: Bot):
    today = date.today()
    due = parse_date(message.text, today)
    if due is None or due < today:
        return await message.answer("Не розумію дату 🤔 Введи у форматі ДД.ММ, наприклад 28.11")
    await finish(bot, message, message.from_user, state, due)


# ---------- Збереження ----------

async def finish(bot: Bot, message: Message, user: User, state: FSMContext, due: date):
    draft = drafts.pop(user.id, None)
    await state.clear()
    if not draft:
        return await message.answer("Чернетку втрачено. Почни знову: /hw")

    text = "\n".join(draft["text"]) or "(див. вкладення)"
    common = dict(group_id=draft["group_id"], subject=draft["subject"], due_date=due,
                  text=text, created_by=user.id, attachments=draft["attachments"])

    # Особисте завдання
    if draft["scope"] == "me":
        hw_id = db.add_homework(**common, owner_user_id=user.id)
        await send_homework(bot, message.chat.id, db.get_homework_by_id(hw_id),
                            header="👤 Особисте завдання збережено ✅")
        return

    chats = db.get_group_chats(draft["group_id"])
    existing = db.find_active_group_homework(draft["group_id"], draft["subject"], due)

    # Групове завдання без конфлікту — одразу чинне
    if not existing:
        hw = db.get_homework_by_id(db.add_homework(**common))
        for chat_id in chats:
            try:
                await send_homework(bot, chat_id, hw,
                                    header=f"➕ {user_link(user)} додав(ла) домашнє завдання")
            except Exception as e:
                logging.warning("Не вдалося надіслати ДЗ у чат %s: %s", chat_id, e)
        note = ("і надіслано в чат групи" if chats else
                "\n⚠️ До групи не прив'язано чат, тому в групу його не надіслано "
                "(староста може написати /create_group у чаті групи)")
        await message.answer(f"👥 Групове завдання збережено {note} ✅")
        return

    # Конфлікт: на цю дату з цього предмета вже є завдання -> рішення за адміністраторами
    if not chats:
        await message.answer(
            "На цю дату з цього предмета вже є групове завдання, а замінити його "
            "може лише адміністратор чату групи. До групи не прив'язано чат (/create_group), "
            "тому заміну зараз неможливо підтвердити.")
        return
    hw_id = db.add_homework(**common, status="pending", replaces_id=existing["id"])
    new_hw = db.get_homework_by_id(hw_id)
    old_attachments = db.get_attachments(existing["id"])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Замінити", callback_data=f"hw_ok:{hw_id}")
    kb.button(text="❌ Залишити як є", callback_data=f"hw_no:{hw_id}")
    header = (f"✏️ {user_link(user)} пропонує замінити домашнє завдання.\n"
              f"<b>Зараз:</b>\n{format_homework(existing, len(old_attachments))}\n\n"
              f"<b>Нове:</b>")
    for chat_id in chats:
        try:
            await send_homework(bot, chat_id, new_hw, header=header, reply_markup=kb.as_markup())
            await bot.send_message(chat_id, "Підтвердити заміну може адміністратор чату 👆")
        except Exception as e:
            logging.warning("Не вдалося надіслати запит на заміну в чат %s: %s", chat_id, e)
    await message.answer("На цю дату з цього предмета вже є завдання. "
                         "Запит на заміну надіслано адміністраторам чату групи ⏳")


# ---------- Рішення адміністратора ----------

@router.callback_query(F.data.startswith(("hw_ok:", "hw_no:")))
async def hw_admin_decision(cb: CallbackQuery, bot: Bot):
    if not await is_chat_admin(bot, cb.message.chat.id, cb.from_user.id):
        return await cb.answer("Підтвердити заміну можуть лише адміністратори чату",
                               show_alert=True)

    approve = cb.data.startswith("hw_ok:")
    hw_id = int(cb.data.split(":")[1])
    if not db.resolve_pending_homework(hw_id, approve):
        await cb.message.edit_reply_markup(reply_markup=None)
        return await cb.answer("Цей запит уже розглянули")

    verdict = ("✅ Замінено" if approve else "❌ Залишено попереднє завдання")
    await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>{verdict}</b> — {user_link(cb.from_user)}",
                               parse_mode="HTML", reply_markup=None)
    await cb.answer()

    hw = db.get_homework_by_id(hw_id)
    if hw["created_by"]:
        result = "прийняв(ла)" if approve else "відхилив(ла)"
        try:
            await bot.send_message(hw["created_by"],
                                   f"Адміністратор {result} твою заміну ДЗ з предмета "
                                   f"<b>{esc(hw['subject'])}</b>.", parse_mode="HTML")
        except Exception:
            pass  # користувач міг заблокувати бота


# ---------- Перегляд ----------

PAGE_SIZE = 8  # завдань на одній сторінці списку


def list_context(chat_type, chat_id: int, user_id: int):
    """Група та власник особистих завдань для списку (у групі особисті не показуються)."""
    if chat_type == ChatType.PRIVATE:
        return db.get_user_group(user_id), user_id
    return db.get_chat_group(chat_id), None


def build_list_page(items, page: int):
    """Текст і кнопки однієї сторінки списку завдань."""
    pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    title = "📝 <b>Домашні завдання на 2 тижні</b>"
    if pages > 1:
        title += f" (сторінка {page + 1}/{pages}, усього {len(items)})"
    lines = [title]
    kb = InlineKeyboardBuilder()
    current = None
    for hw in chunk:
        if hw["due_date"] != current:
            current = hw["due_date"]
            lines.append(f"\n📌 <b>{fmt_date(date.fromisoformat(current))}</b>")
        mark = "👤 " if hw["owner_user_id"] else ""
        files = len(db.get_attachments(hw["id"]))
        clip = f" 📎{files}" if files else ""
        lines.append(f"{mark}<b>{esc(hw['subject'])}</b>: {esc(short(hw['text']))}{clip}")
        due = date.fromisoformat(hw["due_date"]).strftime("%d.%m")
        kb.button(text=f"{mark}{due} · {short(hw['subject'], 35)}{clip}",
                  callback_data=f"hw_show:{hw['id']}")
    sizes = [1] * len(chunk)
    if pages > 1:
        nav = 0
        if page > 0:
            kb.button(text="◀️ Назад", callback_data=f"hw_page:{page - 1}")
            nav += 1
        if page < pages - 1:
            kb.button(text="Далі ▶️", callback_data=f"hw_page:{page + 1}")
            nav += 1
        sizes.append(nav)
    kb.adjust(*sizes)

    if any(hw["owner_user_id"] for hw in chunk):
        lines.append("\n<i>👤 — особисті завдання</i>")
    lines.append("\n👇 Натисни на завдання, щоб отримати його повністю з файлами")
    return "\n".join(lines), kb.as_markup()


def load_list_items(chat_type, chat_id: int, user_id: int):
    group_id, owner = list_context(chat_type, chat_id, user_id)
    if group_id is None:
        return None
    today = date.today()
    return db.get_homework_range(group_id, today, today + timedelta(days=14), owner)


@router.message(Command("hw_list"))
async def hw_list(message: Message):
    items = load_list_items(message.chat.type, message.chat.id, message.from_user.id)
    if items is None:
        return await message.answer("Спершу вкажи групу: /group КН-21 (або /create_group у чаті групи)")
    if not items:
        return await message.answer("Домашніх завдань на найближчі 2 тижні немає 🎉")
    text, markup = build_list_page(items, 0)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.startswith("hw_page:"))
async def hw_list_page(cb: CallbackQuery):
    # Список перебудовується з бази: так на кожній сторінці актуальні дані
    items = load_list_items(cb.message.chat.type, cb.message.chat.id, cb.from_user.id)
    if not items:
        await cb.message.edit_text("Домашніх завдань на найближчі 2 тижні немає 🎉")
        return await cb.answer()
    text, markup = build_list_page(items, int(cb.data.split(":")[1]))
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await cb.answer()


def short(text: str, limit: int = 60) -> str:
    """Скорочує текст для списку: перший рядок, не довше limit символів."""
    first = text.strip().split("\n")[0]
    return first if len(first) <= limit else first[:limit - 1].rstrip() + "…"


@router.callback_query(F.data.startswith("hw_show:"))
async def hw_show(cb: CallbackQuery, bot: Bot):
    hw = db.get_homework_by_id(int(cb.data.split(":")[1]))
    if hw is None or hw["status"] != "active":
        return await cb.answer("Це завдання вже неактуальне (його замінили або видалили). "
                               "Онови список: /hw_list", show_alert=True)

    # Перевірка доступу: особисте завдання бачить лише власник,
    # групове — лише учасники цієї групи
    if cb.message.chat.type == ChatType.PRIVATE:
        allowed = (hw["group_id"] == db.get_user_group(cb.from_user.id) and
                   hw["owner_user_id"] in (None, cb.from_user.id))
    else:
        allowed = (hw["group_id"] == db.get_chat_group(cb.message.chat.id) and
                   hw["owner_user_id"] is None)
    if not allowed:
        return await cb.answer("Немає доступу до цього завдання", show_alert=True)

    await cb.answer()
    header = "👤 Особисте завдання" if hw["owner_user_id"] else ""
    await send_homework(bot, cb.message.chat.id, hw, header=header)