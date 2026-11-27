"""
Перевірка доступу через Telegram.

Членство в академічній групі бот не зберігає окремим списком: він питає
в Telegram, чи є користувач учасником (або адміністратором) чату групи.
Членство перевіряється під час підключення і далі під час кожного звернення
в особистих повідомленнях (результат кешується на MEMBER_CACHE_TTL секунд),
а вихід або видалення з чату одразу закриває доступ.
Так доступ завжди відповідає реальному складу чату, який веде староста.
"""
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramServerError
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

import database as db

MEMBER_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                   ChatMemberStatus.CREATOR}
ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}

MEMBER_CACHE_TTL = 300  # скільки секунд вважати підтверджене членство актуальним
_member_cache: dict[tuple[int, int], float] = {}  # (group_id, user_id) -> час перевірки


class MembershipUnknown(Exception):
    """Telegram тимчасово недоступний, перевірити членство неможливо."""


async def get_member_status(bot: Bot, chat_id: int, user_id: int):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (TelegramNetworkError, TelegramServerError) as e:
        # Збій мережі чи серверів Telegram — це не відповідь «не учасник»
        raise MembershipUnknown(str(e)) from e
    except TelegramAPIError as e:
        # Користувача немає в чаті або бота видалили з чату
        logging.info("getChatMember(%s, %s): %s", chat_id, user_id, e)
        return None
    return member


async def is_chat_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await get_member_status(bot, chat_id, user_id)
    if member is None:
        return False
    if member.status in MEMBER_STATUSES:
        return True
    # Учасник з обмеженнями (restricted) теж учасник, якщо не вийшов із чату
    return member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False)


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await get_member_status(bot, chat_id, user_id)
    except MembershipUnknown:
        return False  # права адміністратора без підтвердження не надаємо
    return member is not None and member.status in ADMIN_STATUSES


async def is_group_admin(bot: Bot, group_id: int, user_id: int) -> bool:
    """Чи є користувач адміністратором хоча б одного чату академічної групи."""
    for chat_id in db.get_group_chats(group_id):
        if await is_chat_admin(bot, chat_id, user_id):
            return True
    return False


async def is_group_member(bot: Bot, group_id: int, user_id: int) -> bool:
    """
    Чи є користувач учасником хоча б одного чату академічної групи.
    Позитивний результат кешується, щоб не звертатися до Telegram на кожне натискання.
    Якщо Telegram недоступний, виникає MembershipUnknown.
    """
    checked = _member_cache.get((group_id, user_id))
    if checked and time.monotonic() - checked < MEMBER_CACHE_TTL:
        return True
    for chat_id in db.get_group_chats(group_id):
        if await is_chat_member(bot, chat_id, user_id):
            _member_cache[(group_id, user_id)] = time.monotonic()
            return True
    return False


def forget_member(group_id: int, user_id: int):
    _member_cache.pop((group_id, user_id), None)


def revoke_access(group_id: int, user_id: int):
    """Відкликає доступ користувача до групи (він більше не учасник чату)."""
    forget_member(group_id, user_id)
    db.leave_group(user_id, group_id)


# ---------- Перевірка при кожному зверненні в особистих повідомленнях ----------

# Команди підключення до групи пропускаються: їх обробники самі перевіряють членство
JOIN_COMMANDS = ("/start", "/group")


class MembershipMiddleware(BaseMiddleware):
    """
    Перед обробкою повідомлення чи натискання кнопки в особистих повідомленнях
    перевіряє, що користувач досі є учасником чату своєї групи.
    Якщо ні — доступ відкликається і обробник не викликається.
    """

    async def __call__(self, handler: Callable[[Any, dict], Awaitable[Any]],
                       event: Message | CallbackQuery, data: dict) -> Any:
        chat = event.chat if isinstance(event, Message) else (
            event.message.chat if event.message else None)
        user = event.from_user
        if chat is None or chat.type != ChatType.PRIVATE or user is None:
            return await handler(event, data)
        group_id = db.get_user_group(user.id)
        if group_id is None:
            return await handler(event, data)  # обробники самі підкажуть, як приєднатися

        try:
            member = await is_group_member(data["bot"], group_id, user.id)
        except MembershipUnknown as e:
            # Telegram недоступний: не відбираємо доступ через тимчасовий збій
            logging.warning("Не вдалося перевірити членство %s: %s", user.id, e)
            return await handler(event, data)
        if member:
            return await handler(event, data)

        group = db.get_group(group_id)
        revoke_access(group_id, user.id)
        logging.info("Користувач %s більше не в чаті групи %s — доступ закрито",
                     user.id, group["code"] if group else group_id)
        if isinstance(event, Message) and (event.text or "").startswith(JOIN_COMMANDS):
            return await handler(event, data)  # /start чи /group: можна приєднатися знову

        if state := data.get("state"):
            await state.clear()
        code = group["code"] if group else ""
        text = (f"Тебе немає в чаті групи {code}, тому доступ до розкладу і завдань закрито 🔒\n"
                "Якщо тебе повернули в чат — натисни кнопку підключення в чаті групи "
                f"або напиши /group {code}")
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)


# ---------- Вихід або видалення з чату групи ----------

router = Router()


@router.chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def member_left(event: ChatMemberUpdated, bot: Bot):
    """
    Telegram повідомляє про вихід учасника, якщо бот є адміністратором чату.
    Тоді доступ закривається одразу, а не під час наступного звернення.
    """
    group_id = db.get_chat_group(event.chat.id)
    user_id = event.new_chat_member.user.id
    if group_id is None or db.get_user_group(user_id) != group_id:
        return
    forget_member(group_id, user_id)
    try:
        # У групи може бути кілька чатів: доступ лишається, якщо людина є в іншому
        if await is_group_member(bot, group_id, user_id):
            return
    except MembershipUnknown:
        pass  # Telegram уже повідомив, що людина вийшла, тож доступ закриваємо
    revoke_access(group_id, user_id)
    logging.info("Користувач %s вийшов із чату групи — доступ закрито", user_id)


def register(dp: Dispatcher):
    """Підключає перевірку членства до диспетчера бота."""
    dp.message.outer_middleware(MembershipMiddleware())
    dp.callback_query.outer_middleware(MembershipMiddleware())
    dp.include_router(router)
