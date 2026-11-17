import sqlite3
from datetime import date

DB_NAME = "database.db"

# Перетворення назви дня (як повертає ШІ) у номер дня тижня (1 = понеділок)
DAY_TO_NUMBER = {
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "sunday": 7,
    "понеділок": 1, "вівторок": 2, "середа": 3, "четвер": 4,
    "п'ятниця": 5, "пʼятниця": 5, "субота": 6, "неділя": 7,
}


def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row          # доступ до полів за назвою: row["subject"]
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite за замовчуванням не перевіряє зовнішні ключі
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript("""
        -- Академічна група, ідентифікується шифром (наприклад, "КН-21")
        CREATE TABLE IF NOT EXISTS groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT NOT NULL UNIQUE,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Користувач бота (студент). user_id = Telegram ID
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            first_name  TEXT,
            username    TEXT,
            group_id    INTEGER REFERENCES groups(id) ON DELETE SET NULL,
            role        TEXT NOT NULL DEFAULT 'student'
                        CHECK (role IN ('student', 'headman')),
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Груповий Telegram-чат, прив'язаний до академічної групи (для розсилок)
        CREATE TABLE IF NOT EXISTS group_chats (
            chat_id     INTEGER PRIMARY KEY,
            group_id    INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            notify      INTEGER NOT NULL DEFAULT 1   -- 1 = розсилки увімкнено
        );

        -- Заняття розкладу. Розклад належить групі, а не окремому користувачу
        CREATE TABLE IF NOT EXISTS lessons (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id     INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            day_of_week  INTEGER NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
            start_time   TEXT NOT NULL,              -- "09:00"
            end_time     TEXT,                       -- "10:20"
            subject      TEXT NOT NULL,
            type         TEXT,                       -- lecture / practice / lab
            teacher      TEXT,
            audience     TEXT,
            week_type    TEXT NOT NULL DEFAULT 'both'
                         CHECK (week_type IN ('both', 'odd', 'even')),  -- чисельник / знаменник
            source       TEXT NOT NULL DEFAULT 'ai'
                         CHECK (source IN ('ai', 'web', 'manual'))
        );

        -- Домашнє завдання, прив'язане до групи, предмета і дати здачі
        CREATE TABLE IF NOT EXISTS homework (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            subject     TEXT NOT NULL,
            due_date    TEXT NOT NULL,               -- "2026-11-18"
            text        TEXT NOT NULL,
            created_by  INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Індекси під найчастіші запити: "пари групи на день" і "ДЗ групи на дату"
        CREATE INDEX IF NOT EXISTS idx_lessons_group_day ON lessons(group_id, day_of_week);
        CREATE INDEX IF NOT EXISTS idx_homework_group_date ON homework(group_id, due_date);
        """)


# ---------- Групи та користувачі ----------

def get_or_create_group(code):
    code = code.strip().upper()
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO groups (code) VALUES (?)", (code,))
        return conn.execute("SELECT id FROM groups WHERE code = ?", (code,)).fetchone()["id"]


def set_user_group(user_id, first_name, username, group_code):
    group_id = get_or_create_group(group_code)
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO users (user_id, first_name, username, group_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username   = excluded.username,
                group_id   = excluded.group_id
        """, (user_id, first_name, username, group_id))
    return group_id


def get_user_group(user_id):
    """Повертає id групи користувача або None, якщо група ще не вказана."""
    with get_connection() as conn:
        row = conn.execute("SELECT group_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["group_id"] if row else None


def bind_chat(chat_id, group_code):
    group_id = get_or_create_group(group_code)
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO group_chats (chat_id, group_id) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET group_id = excluded.group_id
        """, (chat_id, group_id))
    return group_id


def get_chats_for_notify():
    with get_connection() as conn:
        return conn.execute("SELECT chat_id, group_id FROM group_chats WHERE notify = 1").fetchall()


# ---------- Розклад ----------

def save_lessons(data, group_id, source="ai"):
    """Замінює розклад групи на новий (формат JSON, який повертає ШІ)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM lessons WHERE group_id = ?", (group_id,))
        for day_item in data["schedule"]:
            day_number = DAY_TO_NUMBER.get(str(day_item["day"]).strip().lower())
            if day_number is None:
                continue  # невідомий день — пропускаємо
            for sub in day_item["subjects"]:
                conn.execute("""
                    INSERT INTO lessons (group_id, day_of_week, start_time, end_time,
                                         subject, type, teacher, audience, week_type, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    group_id, day_number,
                    sub.get("time"), sub.get("end_time"),
                    sub.get("name"), sub.get("type"),
                    sub.get("teacher"), sub.get("audience"),
                    sub.get("week_type") or "both", source,
                ))


def get_lessons(group_id, day_of_week, week_type="both"):
    """Пари групи на день з урахуванням чисельника/знаменника."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM lessons
            WHERE group_id = ? AND day_of_week = ?
              AND (week_type = 'both' OR week_type = ?)
            ORDER BY start_time
        """, (group_id, day_of_week, week_type)).fetchall()


# ---------- Домашнє завдання ----------

def add_homework(group_id, subject, due_date, text, created_by):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO homework (group_id, subject, due_date, text, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (group_id, subject, str(due_date), text, created_by))


def get_homework(group_id, due_date: date):
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM homework WHERE group_id = ? AND due_date = ?
            ORDER BY subject
        """, (group_id, str(due_date))).fetchall()