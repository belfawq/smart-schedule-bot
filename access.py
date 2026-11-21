"""
Перевірка доступу через Telegram.

Членство в академічній групі бот не зберігає окремим списком: він питає
в Telegram, чи є користувач учасником (або адміністратором) чату групи.
Так доступ завжди відповідає реальному складу чату, який веде староста.
"""
import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

import database as db

MEMBER_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                   ChatMemberStatus.CREATOR}
ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


async def get_member_status(bot: Bot, chat_id: int, user_id: int):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
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
    member = await get_member_status(bot, chat_id, user_id)
    return member is not None and member.status in ADMIN_STATUSES


async def is_group_admin(bot: Bot, group_id: int, user_id: int) -> bool:
    """Чи є користувач адміністратором хоча б одного чату академічної групи."""
    for chat_id in db.get_group_chats(group_id):
        if await is_chat_admin(bot, chat_id, user_id):
            return True
    return False