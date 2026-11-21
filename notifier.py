"""
Планувальник розсилок і автоматичного оновлення розкладу.

Розклад дій (київський час, змінюється у файлі .env):
  06:30 і 18:30  — оновлення розкладу всіх груп з АСУ «ВНЗ»
                   (перед кожною розсилкою дані свіжі);
  08:00          — у чат групи: розклад на сьогодні і ДЗ, яке здаємо сьогодні;
                   у ЛС: особисті завдання на сьогодні;
  19:00          — у чат групи: ДЗ на завтра (лише якщо воно є);
                   у ЛС: нагадування про особисті завдання на завтра.
"""
import asyncio
import logging
import os
from collections import defaultdict
from datetime import date, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import clock
import database as db
from access import is_chat_admin
from formatting import escape, format_day_title, format_lessons
from homework import send_homework
from parser import ScheduleSourceError
from schedule_sync import sync_group

router = Router()
GROUP_CHATS = {ChatType.GROUP, ChatType.SUPERGROUP}


def parse_time(value: str, default: str) -> tuple[int, int]:
    try:
        hours, minutes = (value or default).split(":")
        return int(hours), int(minutes)
    except ValueError:
        logging.warning("Некоректний час %r у .env, використовую %s", value, default)
        hours, minutes = default.split(":")
        return int(hours), int(minutes)


MORNING = parse_time(os.getenv("MORNING_TIME"), "08:00")
EVENING = parse_time(os.getenv("EVENING_TIME"), "19:00")
SYNC_TIMES = [(6, 30), (18, 30)]
SEND_DELAY = 0.1  # пауза між повідомленнями, щоб не впертися в ліміти Telegram


async def safe_send(coro, target):
    """Надсилання, яке не зупиняє розсилку, якщо один чат недоступний."""
    try:
        await coro
    except TelegramAPIError as e:
        # бота видалили з чату або користувач заблокував бота
        logging.warning("Не вдалося надіслати в %s: %s", target, e)
    await asyncio.sleep(SEND_DELAY)


# ---------- Оновлення розкладу ----------

async def sync_all_groups():
    groups = db.get_groups_for_sync()
    ok = 0
    for group in groups:
        try:
            count, _, _ = await sync_group(group["id"])
            ok += 1
            logging.info("Розклад групи %s оновлено: %d занять", group["code"], count)
        except ScheduleSourceError as e:
            # Сервер недоступний — у базі лишається попередній розклад
            logging.warning("Не вдалося оновити розклад групи %s: %s", group["code"], e)
        await asyncio.sleep(1)  # не навантажуємо сервер АСУ «ВНЗ» запитами підряд
    logging.info("Оновлення розкладу: успішно %d з %d груп", ok, len(groups))


# ---------- Ранкова розсилка ----------

def morning_text(group_id: int, day: date):
    """Текст ранкової розсилки або None, якщо сьогодні немає ні пар, ні ДЗ."""
    lessons = db.get_lessons(group_id, day)
    homework = db.get_homework(group_id, day)
    if not lessons and not homework:
        return None
    text = "☀️ <b>Доброго ранку!</b>\n\n" + format_lessons(day, lessons)
    if homework:
        text += "\n\n📝 <b>Сьогодні здаємо:</b>"
        for hw in homework:
            files = len(db.get_attachments(hw["id"]))
            clip = f" 📎{files}" if files else ""
            text += f"\n• <b>{escape(hw['subject'])}</b>: {escape(hw['text'])}{clip}"
        if any(db.get_attachments(hw["id"]) for hw in homework):
            text += "\n\n<i>Файли до завдань: /hw_list</i>"
    return text


async def send_morning(bot: Bot, chat_id: int, group_id: int, day: date, force: bool = False):
    text = morning_text(group_id, day)
    if text is None:
        if force:
            await bot.send_message(chat_id, f"На {format_day_title(day)} пар і ДЗ немає 🎉")
        return
    await safe_send(bot.send_message(chat_id, text, parse_mode="HTML"), chat_id)


async def morning_broadcast(bot: Bot):
    day = clock.today()
    for chat_id, group_id in db.get_chats_for_notify():
        await send_morning(bot, chat_id, group_id, day)
    await send_personal(bot, day, "📌 <b>Сьогодні треба здати (особисте):</b>")


# ---------- Вечірня розсилка ----------

async def send_evening(bot: Bot, chat_id: int, group_id: int, day: date, force: bool = False):
    homework = db.get_homework(group_id, day)
    if not homework:
        if force:
            await bot.send_message(chat_id, f"На {format_day_title(day)} домашнього завдання немає 🎉")
        return
    await safe_send(bot.send_message(
        chat_id, f"🌙 <b>Домашнє завдання на завтра ({format_day_title(day)})</b>",
        parse_mode="HTML"), chat_id)
    for hw in homework:  # кожне завдання окремо, разом із файлами
        await safe_send(send_homework(bot, chat_id, hw), chat_id)


async def evening_broadcast(bot: Bot):
    day = clock.today() + timedelta(days=1)
    for chat_id, group_id in db.get_chats_for_notify():
        await send_evening(bot, chat_id, group_id, day)
    await send_personal(bot, day, f"⏰ <b>Нагадування: завтра ({format_day_title(day)}) "
                                  "треба здати (особисте):</b>")


# ---------- Особисті нагадування ----------

async def send_personal(bot: Bot, day: date, header: str):
    by_user = defaultdict(list)
    for hw in db.get_personal_homework_on(day):
        by_user[hw["owner_user_id"]].append(hw)
    for user_id, items in by_user.items():
        await safe_send(bot.send_message(user_id, header, parse_mode="HTML"), user_id)
        for hw in items:
            await safe_send(send_homework(bot, user_id, hw), user_id)


# ---------- Запуск планувальника ----------

def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    # misfire_grace_time: якщо комп'ютер спав о 08:00 і прокинувся до 09:00,
    # розсилка все одно відбудеться; coalesce: пропущені запуски не дублюються
    common = dict(misfire_grace_time=3600, coalesce=True, max_instances=1)
    scheduler = AsyncIOScheduler(timezone=clock.TZ)
    for i, (h, m) in enumerate(SYNC_TIMES):
        scheduler.add_job(sync_all_groups, CronTrigger(hour=h, minute=m, timezone=clock.TZ),
                          id=f"sync_{i}", **common)
    scheduler.add_job(morning_broadcast, CronTrigger(hour=MORNING[0], minute=MORNING[1],
                                                     timezone=clock.TZ),
                      args=[bot], id="morning", **common)
    scheduler.add_job(evening_broadcast, CronTrigger(hour=EVENING[0], minute=EVENING[1],
                                                     timezone=clock.TZ),
                      args=[bot], id="evening", **common)
    return scheduler


# ---------- Команди адміністраторів чату ----------

@router.message(Command("notify"), F.chat.type.in_(GROUP_CHATS))
async def notify_command(message: Message, command: CommandObject, bot: Bot):
    if db.get_chat_group(message.chat.id) is None:
        return await message.answer("Спершу створи групу: /create_group")
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        state = "увімкнені ✅" if db.get_chat_notify(message.chat.id) else "вимкнені ❌"
        return await message.answer(
            f"Розсилки в цьому чаті {state}\n"
            f"☀️ {MORNING[0]:02d}:{MORNING[1]:02d} — розклад і ДЗ на сьогодні\n"
            f"🌙 {EVENING[0]:02d}:{EVENING[1]:02d} — ДЗ на завтра (якщо є)\n\n"
            "Увімкнути: /notify on, вимкнути: /notify off\n"
            "Подивитися зараз: /morning, /evening")
    if not await is_chat_admin(bot, message.chat.id, message.from_user.id):
        return await message.answer("Змінювати розсилки може лише адміністратор чату.")
    db.set_chat_notify(message.chat.id, arg == "on")
    await message.answer("Розсилки увімкнено ✅" if arg == "on" else "Розсилки вимкнено ❌")


@router.message(Command("morning"), F.chat.type.in_(GROUP_CHATS))
async def morning_now(message: Message, bot: Bot):
    """Показати ранкову розсилку зараз (для перевірки)."""
    group_id = db.get_chat_group(message.chat.id)
    if group_id is None:
        return await message.answer("Спершу створи групу: /create_group")
    await send_morning(bot, message.chat.id, group_id, clock.today(), force=True)


@router.message(Command("evening"), F.chat.type.in_(GROUP_CHATS))
async def evening_now(message: Message, bot: Bot):
    """Показати вечірню розсилку зараз (для перевірки)."""
    group_id = db.get_chat_group(message.chat.id)
    if group_id is None:
        return await message.answer("Спершу створи групу: /create_group")
    await send_evening(bot, message.chat.id, group_id, clock.today() + timedelta(days=1),
                       force=True)