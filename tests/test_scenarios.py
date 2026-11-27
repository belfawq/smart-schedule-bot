"""
Функціональне тестування сценаріїв користувача (для підрозділу 3.3).

Бот запускається з імітацією серверів Telegram, АСУ «ВНЗ» і Gemini: повідомлення
не надсилаються насправді, а перехоплюються і перевіряються. Використовується окрема
тимчасова база, робоча база і чати групи не зачіпаються. Інтернет не потрібен.

Запуск:
  python tests/test_scenarios.py
"""
import asyncio
import datetime as dt
import os
import traceback
from datetime import timedelta

from common import RESULTS, print_table, save_csv

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("AI_API_KEY", "test")

import database as db  # noqa: E402

TMP_DB = RESULTS / "scenarios.db"
TMP_DB.unlink(missing_ok=True)
db.DB_NAME = str(TMP_DB)

import access  # noqa: E402
import clock  # noqa: E402
import homework  # noqa: E402
import main  # noqa: E402
import notifier  # noqa: E402
import schedule_sync  # noqa: E402
import site_link  # noqa: E402
from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from aiogram.methods import (AnswerCallbackQuery, GetChatMember, GetMe,  # noqa: E402
                             SendMediaGroup)
from aiogram.types import (ChatMemberAdministrator, ChatMemberMember, Message,  # noqa: E402
                           Update, User)
from parser import ScheduleSourceError  # noqa: E402

# ---------- Імітація Telegram ----------
ADMIN, STUDENT, STUDENT2, STRANGER = 100, 200, 300, 999
CHAT = -1001
MEMBERS = {ADMIN, STUDENT, STUDENT2}
sent = []  # (chat_id, текст, кнопки, тип запиту)


async def fake_telegram(self, method, request_timeout=None):
    if isinstance(method, GetMe):
        return User(id=1, is_bot=True, first_name="DueDone", username="duedone_bot")
    if isinstance(method, GetChatMember):
        user = User(id=method.user_id, is_bot=False, first_name="U")
        if method.user_id == ADMIN:
            return ChatMemberAdministrator.model_construct(status="administrator", user=user)
        if method.user_id in MEMBERS:
            return ChatMemberMember.model_construct(status="member", user=user)
        raise TelegramBadRequest(method=method, message="user not found")
    kb = getattr(method, "reply_markup", None)
    buttons = [b.text for r in getattr(kb, "inline_keyboard", []) or [] for b in r]
    text = getattr(method, "text", None) or ""
    sent.append((getattr(method, "chat_id", None), text, buttons, type(method).__name__,
                 getattr(method, "show_alert", None)))
    if isinstance(method, SendMediaGroup):
        return []
    if isinstance(method, AnswerCallbackQuery):
        return True
    return Message(message_id=len(sent), date=dt.datetime.now(),
                   chat={"id": getattr(method, "chat_id", None) or 1, "type": "private"},
                   text=text).as_(self)

Bot.__call__ = fake_telegram

# Імітація розпізнавання (Gemini) і АСУ «ВНЗ»
TODAY = clock.today()
TOMORROW = TODAY + timedelta(days=1)


async def fake_parse(data, is_image):
    return {"schedule": [{"day": "", "date": str(TOMORROW), "subjects": [
        {"name": "Операційні системи", "time": "08:30", "end_time": "09:50", "type": "lecture",
         "teacher": None, "audience": "ауд. 101", "subgroup": None}]}]}

server_up = True


async def fake_fetch(site_id, start, end):
    if not server_up:
        raise ScheduleSourceError("Сервер розкладу недоступний")
    return {"schedule": [{"date": str(TODAY), "subjects": [
        {"name": "Об'єктно-орієнтоване програмування", "time": "08:30", "end_time": "09:50",
         "type": "lab", "teacher": None, "audience": "ауд. 29, корп. 4", "subgroup": "1"}]},
        {"date": str(TOMORROW), "subjects": [
        {"name": "Об'єктно-орієнтоване програмування", "time": "10:10", "end_time": "11:30",
         "type": "lecture", "teacher": None, "audience": None, "subgroup": None}]}]}

main.parse_schedule = fake_parse
schedule_sync.fetch_schedule = fake_fetch

# ---------- Допоміжні функції ----------
_uid = [0]


def msg(text=None, user=STUDENT, chat=None, photo=None):
    _uid[0] += 1
    chat = chat or user
    m = {"message_id": _uid[0], "date": 0,
         "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
         "from": {"id": user, "is_bot": False, "first_name": f"U{user}"}}
    if text:
        m["text"] = text
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if photo:
        m["photo"] = [{"file_id": photo, "file_unique_id": photo, "width": 1, "height": 1}]
    return Update(update_id=_uid[0], message=m)


def cb(data, user=STUDENT, chat=None):
    _uid[0] += 1
    chat = chat or user
    return Update(update_id=_uid[0], callback_query={
        "id": str(_uid[0]), "from": {"id": user, "is_bot": False, "first_name": f"U{user}"},
        "chat_instance": "x", "data": data,
        # date має бути ненульовим: повідомлення з date = 0 aiogram вважає
        # недоступним (InaccessibleMessage), і кнопки під ним не обробляються
        "message": {"message_id": 1, "date": 1, "text": "m",
                    "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"}}})


bot = Bot(token=os.environ["BOT_TOKEN"])
dp = Dispatcher()
access.register(dp)
dp.include_routers(main.menu_router, homework.router, site_link.router, notifier.router,
                   main.common_router, main.private_router, main.group_router)


async def send(*updates):
    sent.clear()
    for u in updates:
        await dp.feed_update(bot, u)
    return list(sent)


def texts(out, chat=None):
    return " ".join(t for c, t, *_ in out if chat is None or c == chat)


def alerts(out):
    return [t for c, t, b, kind, alert in out if kind == "AnswerCallbackQuery" and alert]


# ---------- Сценарії ----------
results = []


def scenario(title, expected):
    def wrap(fn):
        results.append((title, expected, fn))
        return fn
    return wrap


@scenario("Створення групи учасником без прав адміністратора",
          "Відмова: створити групу може лише адміністратор")
async def s1():
    out = await send(msg("/create_group КН1-Б25", user=STUDENT, chat=CHAT))
    assert "лише адміністратор" in texts(out)


@scenario("Створення групи адміністратором чату",
          "Групу створено, у чаті кнопка «Підключитися до бота»")
async def s2():
    out = await send(msg("/create_group КН1-Б25", user=ADMIN, chat=CHAT))
    assert "створено" in texts(out) and any("Підключитися" in b for _, _, bs, *_ in out for b in bs)
    db.set_site_group_id(db.get_chat_group(CHAT), "TEST", "КН1-Б25")


@scenario("Підключення учасника чату за кнопкою", "Доступ надано, з'явилось меню")
async def s3():
    gid = db.get_chat_group(CHAT)
    out = await send(msg(f"/start join_{gid}", user=STUDENT), msg(f"/start join_{gid}", user=ADMIN),
                     msg("/group кн1-б25", user=STUDENT2))
    assert texts(out).count("Тебе додано") == 3


@scenario("Спроба підключення людини не з чату групи", "Відмова в доступі")
async def s4():
    out = await send(msg("/group KN1-B25", user=STRANGER))
    assert "не учасник чату" in texts(out) and db.get_user_group(STRANGER) is None


@scenario("Оновлення розкладу з АСУ «ВНЗ» (/update)", "Розклад завантажено в базу")
async def s5():
    out = await send(msg("/update", user=STUDENT))
    assert "оновлено" in texts(out) and db.get_lessons(db.get_chat_group(CHAT), TODAY)


@scenario("Сервер АСУ «ВНЗ» недоступний", "Повідомлення про помилку, попередній розклад збережено")
async def s6():
    global server_up
    server_up = False
    out = await send(msg("/update", user=STUDENT))
    server_up = True
    assert "Не вдалося" in texts(out) and db.get_lessons(db.get_chat_group(CHAT), TODAY)


@scenario("Розклад на сьогодні (/today) у ЛС і в чаті", "Показано пару з підгрупою та аудиторією")
async def s7():
    out = await send(msg("/today", user=STUDENT), msg("/today", user=STUDENT, chat=CHAT))
    t = texts(out)
    assert t.count("Об'єктно-орієнтоване програмування") == 2 and "1 підгрупа" in t


@scenario("Розклад на тиждень (/week)", "Показано тиждень з понеділка по неділю")
async def s8():
    out = await send(msg("🗓 Тиждень", user=STUDENT))
    monday, sunday = main.week_bounds(TODAY)
    assert f"{monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}" in texts(out)


@scenario("Завантаження розкладу з фото студентом", "Відмова: лише адміністратор")
async def s9():
    out = await send(msg("/add", user=STUDENT))
    assert "лише адміністратори" in texts(out)


@scenario("Завантаження розкладу текстом адміністратором",
          "Попередній перегляд, збереження після «Зберегти»")
async def s10():
    out = await send(msg("/add", user=ADMIN), msg("пн 08:30 ОС", user=ADMIN))
    assert "Ось що я розпізнав" in texts(out) and "✅ Зберегти" in out[-1][2]
    out = await send(cb("sched_save", user=ADMIN))
    lessons = db.get_lessons(db.get_chat_group(CHAT), TOMORROW)
    assert "збережено" in texts(out) and any(l["source"] == "ai" for l in lessons)


@scenario("Особисте домашнє завдання з дедлайном «на наступну пару»",
          "Збережено, видно лише автору")
async def s11():
    gid = db.get_chat_group(CHAT)
    subjects = db.get_group_subjects(gid, TODAY - timedelta(days=14), TODAY + timedelta(days=30))
    idx = subjects.index("Операційні системи")  # предмет, пара з якого є завтра
    out = await send(msg("📝 Додати ДЗ", user=STUDENT), cb("hw_scope:me"), cb(f"hw_subj:{idx}"),
                     msg("Прочитати конспект", user=STUDENT), cb("hw_done"), cb("hw_date:next"))
    assert "Особисте завдання збережено" in texts(out)
    other = await send(msg("/hw_list", user=STUDENT2))
    assert "Прочитати конспект" not in texts(other)


@scenario("Групове домашнє завдання з фото", "Збережено і надіслано в чат групи з фото")
async def s12():
    out = await send(msg("/hw", user=STUDENT), cb("hw_scope:group"), cb("hw_subj:0"),
                     msg(user=STUDENT, photo="PHOTO1"), msg("Лабораторна №5", user=STUDENT),
                     cb("hw_done"), cb("hw_date:other"),
                     msg(TOMORROW.strftime("%d.%m"), user=STUDENT))
    assert "Лабораторна №5" in texts(out, CHAT) and any(c == CHAT and k == "SendPhoto"
                                                        for c, _, _, k, _ in out)


@scenario("Повторне групове ДЗ на ту саму дату з того самого предмета",
          "Не замінює одразу: запит адміністраторам з кнопками")
async def s13():
    out = await send(msg("/hw", user=STUDENT2), cb("hw_scope:group", user=STUDENT2),
                     cb("hw_subj:0", user=STUDENT2), msg("Лабораторна №6", user=STUDENT2),
                     cb("hw_done", user=STUDENT2), cb("hw_date:other", user=STUDENT2),
                     msg(TOMORROW.strftime("%d.%m"), user=STUDENT2))
    assert "пропонує замінити" in texts(out, CHAT)
    assert any("Замінити" in b for c, _, bs, *_ in out if c == CHAT for b in bs)


@scenario("Підтвердження заміни: студент, потім адміністратор, потім повторне натискання",
          "Студенту відмова, адміністратор замінює, повтор ігнорується")
async def s14():
    with db.get_connection() as conn:
        hw_id = conn.execute("SELECT id FROM homework WHERE status='pending'").fetchone()[0]
    out = await send(cb(f"hw_ok:{hw_id}", user=STUDENT, chat=CHAT))
    assert alerts(out)
    out = await send(cb(f"hw_ok:{hw_id}", user=ADMIN, chat=CHAT))
    assert "Замінено" in texts(out)
    out = await send(cb(f"hw_ok:{hw_id}", user=ADMIN, chat=CHAT))
    assert db.get_homework_by_id(hw_id)["status"] == "active"
    gid = db.get_chat_group(CHAT)
    assert [h["text"] for h in db.get_homework(gid, TOMORROW)] == ["Лабораторна №6"]


@scenario("Список ДЗ з кнопками і відкриття завдання повністю",
          "Список з кнопками, завдання надіслано повністю")
async def s15():
    out = await send(msg("📋 Список ДЗ", user=STUDENT))
    assert out[-1][2], "немає кнопок"
    gid = db.get_chat_group(CHAT)
    hw = db.get_homework(gid, TOMORROW)[0]
    out = await send(cb(f"hw_show:{hw['id']}", user=STUDENT))
    assert "Лабораторна №6" in texts(out)


@scenario("Видалення групового ДЗ студентом і адміністратором",
          "Студенту відмова, адміністратор видаляє після підтвердження")
async def s16():
    gid = db.get_chat_group(CHAT)
    hw = db.get_homework(gid, TOMORROW)[0]
    out = await send(cb(f"hw_del:{hw['id']}", user=STUDENT, chat=CHAT))
    assert alerts(out)
    out = await send(cb(f"hw_del:{hw['id']}", user=ADMIN, chat=CHAT),
                     cb(f"hw_delok:{hw['id']}", user=ADMIN, chat=CHAT))
    assert db.get_homework_by_id(hw["id"]) is None


@scenario("Ранкова розсилка", "У чат: розклад на сьогодні; у ЛС: особисті ДЗ")
async def s17():
    gid = db.get_chat_group(CHAT)
    db.add_homework(gid, "Англійська мова", TODAY, "Слова", STUDENT, owner_user_id=STUDENT)
    sent.clear()
    await notifier.morning_broadcast(bot)
    assert "Доброго ранку" in texts(sent, CHAT) and "Слова" in texts(sent, STUDENT)


@scenario("Вечірня розсилка без ДЗ на завтра", "У чат нічого не надсилається")
async def s18():
    gid = db.get_chat_group(CHAT)
    for h in db.get_homework(gid, TOMORROW):
        db.delete_homework(h["id"])
    sent.clear()
    await notifier.evening_broadcast(bot)
    assert not texts(sent, CHAT)


@scenario("Вечірня розсилка з ДЗ і вимкнення розсилок (/notify off)",
          "Із ДЗ — надсилається; після /notify off — ні")
async def s19():
    gid = db.get_chat_group(CHAT)
    db.add_homework(gid, "Вища математика", TOMORROW, "Задачі 1–10", STUDENT)
    sent.clear()
    await notifier.evening_broadcast(bot)
    assert "Задачі 1–10" in texts(sent, CHAT)
    await send(msg("/notify off", user=ADMIN, chat=CHAT))
    sent.clear()
    await notifier.evening_broadcast(bot)
    assert not texts(sent, CHAT)


@scenario("Натискання «На наступну пару», коли пари з предмета вже немає",
          "Попередження, бот не падає, можна обрати іншу дату")
async def s20():
    gid = db.get_chat_group(CHAT)
    subjects = db.get_group_subjects(gid, TODAY - timedelta(days=14), TODAY + timedelta(days=30))
    idx = subjects.index("Об'єктно-орієнтоване програмування")  # пар попереду немає
    out = await send(msg("/hw", user=STUDENT), cb("hw_scope:me"), cb(f"hw_subj:{idx}"),
                     msg("Повторити тему", user=STUDENT), cb("hw_done"), cb("hw_date:next"))
    assert alerts(out) and "Інша дата" in alerts(out)[0]


@scenario("Незрозуміле повідомлення в ЛС", "Підказка зі списком команд і меню")
async def s21():
    await send(cb("hw_cancel"))
    out = await send(msg("а де дз?", user=STUDENT))
    assert "Не зрозумів" in texts(out)


@scenario("Учасника видалили з чату групи: доступ і повернення",
          "Доступ закрито, після повернення в чат — відкрито знову")
async def s22():
    gid = db.get_chat_group(CHAT)
    MEMBERS.discard(STUDENT2)
    access.forget_member(gid, STUDENT2)  # як після закінчення терміну кешу
    try:
        out = await send(msg("/today", user=STUDENT2))
        assert "доступ до розкладу і завдань закрито" in texts(out)
        assert db.get_user_group(STUDENT2) is None
        out = await send(msg("/week", user=STUDENT2))  # повторно — як для нового користувача
        assert "Спершу" in texts(out)
        # Telegram повідомляє про видалення іншого учасника — доступ закривається одразу
        MEMBERS.discard(STUDENT)
        await send(Update(update_id=10_000, chat_member={
            "chat": {"id": CHAT, "type": "supergroup"},
            "from": {"id": ADMIN, "is_bot": False, "first_name": "A"}, "date": 1,
            "old_chat_member": {"status": "member", "user": {"id": STUDENT, "is_bot": False,
                                                             "first_name": "S"}},
            "new_chat_member": {"status": "kicked", "until_date": 0,
                                "user": {"id": STUDENT, "is_bot": False, "first_name": "S"}}}))
        assert db.get_user_group(STUDENT) is None
    finally:
        MEMBERS.update({STUDENT, STUDENT2})
    out = await send(msg("/group КН1-Б25", user=STUDENT2), msg(f"/start join_{gid}", user=STUDENT))
    assert texts(out).count("Тебе додано") == 2 and db.get_user_group(STUDENT2) == gid


async def run():
    db.init_db()
    rows = []
    for i, (title, expected, fn) in enumerate(results, start=1):
        try:
            await fn()
            status = "Пройдено"
        except Exception as e:
            status = "НЕ пройдено"
            print(f"\n[{i}] {title}: {e!r}")
            traceback.print_exc()
        rows.append([i, title, expected, status])
    headers = ["№", "Сценарій", "Очікуваний результат", "Результат"]
    print()
    print_table(headers, rows)
    passed = sum(r[3] == "Пройдено" for r in rows)
    print(f"\nПройдено {passed} з {len(rows)} сценаріїв")
    save_csv("scenarios", headers, rows)
    TMP_DB.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(run())