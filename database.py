import sqlite3
from datetime import date

DB_NAME = "database.db"

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
            site_group_id TEXT,                     -- aStudyGroupID групи в АСУ «ВНЗ»
            site_group_name TEXT,                   -- назва групи в АСУ «ВНЗ»
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

        -- Заняття розкладу. Розклад належить групі і прив'язаний до конкретної дати,
        -- бо розклад щотижня новий (публікується наперед на наступний тиждень)
        CREATE TABLE IF NOT EXISTS lessons (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id     INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            lesson_date  TEXT NOT NULL,              -- "2026-11-19"
            start_time   TEXT NOT NULL,              -- "08:30"
            end_time     TEXT,                       -- "09:50"
            subject      TEXT NOT NULL,
            type         TEXT,                       -- lecture / practice / lab
            teacher      TEXT,
            audience     TEXT,
            subgroup     TEXT,                       -- "1", "2", "1а"; NULL - уся група
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
            -- NULL - завдання для всієї групи; user_id - особисте завдання студента
            owner_user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
            -- active - чинне; pending - чекає підтвердження адміністратора;
            -- replaced - замінене новим; rejected - заміну відхилено
            status      TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'pending', 'replaced', 'rejected')),
            replaces_id INTEGER REFERENCES homework(id) ON DELETE SET NULL,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Вкладення до завдання: файли зберігає Telegram, у базі лише їхні file_id
        CREATE TABLE IF NOT EXISTS homework_attachments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            homework_id  INTEGER NOT NULL REFERENCES homework(id) ON DELETE CASCADE,
            file_id      TEXT NOT NULL,
            file_type    TEXT NOT NULL CHECK (file_type IN ('photo', 'document', 'video'))
        );

        -- Індекси під найчастіші запити: "пари групи на день" і "ДЗ групи на дату"
        CREATE INDEX IF NOT EXISTS idx_lessons_group_date ON lessons(group_id, lesson_date);
        CREATE INDEX IF NOT EXISTS idx_homework_group_date ON homework(group_id, due_date);
        """)
        # Міграція для баз, створених до появи вебпарсингу
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(groups)")]
        if "site_group_id" not in columns:
            conn.execute("ALTER TABLE groups ADD COLUMN site_group_id TEXT")
        if "site_group_name" not in columns:
            conn.execute("ALTER TABLE groups ADD COLUMN site_group_name TEXT")
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(homework)")]
        if "owner_user_id" not in columns:
            conn.execute("ALTER TABLE homework ADD COLUMN owner_user_id INTEGER "
                         "REFERENCES users(user_id) ON DELETE CASCADE")
            conn.execute("ALTER TABLE homework ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            conn.execute("ALTER TABLE homework ADD COLUMN replaces_id INTEGER "
                         "REFERENCES homework(id) ON DELETE SET NULL")
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(lessons)")]
        if "subgroup" not in columns:
            conn.execute("ALTER TABLE lessons ADD COLUMN subgroup TEXT")


# ---------- Групи та користувачі ----------

# Транслітерація для порівняння шифрів: «КН1-Б25» і «KN1-B25» вважаються однаковими
_TRANSLIT = str.maketrans({
    "А": "A", "Б": "B", "В": "V", "Г": "H", "Ґ": "G", "Д": "D", "Е": "E", "Є": "YE",
    "Ж": "ZH", "З": "Z", "И": "Y", "І": "I", "Ї": "YI", "Й": "Y", "К": "K", "Л": "L",
    "М": "M", "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "KH", "Ц": "TS", "Ч": "CH", "Ш": "SH", "Щ": "SHCH", "Ь": "", "Ю": "YU",
    "Я": "YA", "'": "", "’": "", "ʼ": "", " ": "", "_": "-",
})


def group_key(code: str) -> str:
    return code.strip().upper().translate(_TRANSLIT)


def get_group_by_code(code):
    """Шукає групу за шифром без урахування регістру й алфавіту (кирилиця/латиниця)."""
    key = group_key(code)
    with get_connection() as conn:
        for row in conn.execute("SELECT * FROM groups"):
            if group_key(row["code"]) == key:
                return row
    return None


def create_group(code):
    """Створює групу (або повертає наявну з таким самим шифром). Повертає id."""
    existing = get_group_by_code(code)
    if existing:
        return existing["id"]
    with get_connection() as conn:
        return conn.execute("INSERT INTO groups (code) VALUES (?)",
                            (code.strip().upper(),)).lastrowid


def join_group(user_id, first_name, username, group_id):
    """Прив'язує користувача до групи (після перевірки членства в чаті групи)."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO users (user_id, first_name, username, group_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username   = excluded.username,
                group_id   = excluded.group_id
        """, (user_id, first_name, username, group_id))


def set_site_group_id(group_id, site_group_id, site_group_name=None):
    """Зберігає ідентифікатор (aStudyGroupID) і назву групи в АСУ «ВНЗ»."""
    with get_connection() as conn:
        conn.execute("UPDATE groups SET site_group_id = ?, site_group_name = ? WHERE id = ?",
                     (site_group_id, site_group_name, group_id))


def get_group(group_id):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def get_groups_for_sync():
    """Групи, для яких відомий ідентифікатор в АСУ «ВНЗ»."""
    with get_connection() as conn:
        return conn.execute("SELECT * FROM groups WHERE site_group_id IS NOT NULL").fetchall()


def get_user_group(user_id):
    """Повертає id групи користувача або None, якщо група ще не вказана."""
    with get_connection() as conn:
        row = conn.execute("SELECT group_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["group_id"] if row else None


def bind_chat(chat_id, group_id):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO group_chats (chat_id, group_id) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET group_id = excluded.group_id
        """, (chat_id, group_id))


def get_chat_group(chat_id):
    """Повертає id групи, до якої прив'язано груповий чат, або None."""
    with get_connection() as conn:
        row = conn.execute("SELECT group_id FROM group_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row["group_id"] if row else None


def set_chat_notify(chat_id, enabled: bool):
    with get_connection() as conn:
        conn.execute("UPDATE group_chats SET notify = ? WHERE chat_id = ?", (int(enabled), chat_id))


def get_chat_notify(chat_id) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT notify FROM group_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return bool(row and row["notify"])


def get_chats_for_notify():
    with get_connection() as conn:
        return conn.execute("SELECT chat_id, group_id FROM group_chats WHERE notify = 1").fetchall()


# ---------- Розклад ----------

def save_lessons(data, group_id, source="ai"):
    """
    Зберігає розклад групи. data = {"schedule": [{"date": "2026-11-19", "subjects": [...]}, ...]}.
    Перезаписуються тільки ті дати, які є в новому розкладі,
    тому розклад наступного тижня не стирає поточний.
    Повертає список збережених дат.
    """
    dates = [str(day["date"]) for day in data["schedule"]]
    with get_connection() as conn:
        conn.executemany("DELETE FROM lessons WHERE group_id = ? AND lesson_date = ?",
                         [(group_id, d) for d in dates])
        for day in data["schedule"]:
            for sub in day["subjects"]:
                conn.execute("""
                    INSERT INTO lessons (group_id, lesson_date, start_time, end_time,
                                         subject, type, teacher, audience, subgroup, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    group_id, str(day["date"]),
                    sub.get("time"), sub.get("end_time"),
                    sub.get("name"), sub.get("type"),
                    sub.get("teacher"), sub.get("audience"), sub.get("subgroup"), source,
                ))
    return sorted(dates)


def replace_lessons_range(data, group_id, start: date, end: date, source="web"):
    """
    Замінює розклад групи за весь період [start; end] одним транзакційним кроком.
    На відміну від save_lessons, видаляє й ті дати, яких немає в нових даних:
    якщо пару скасували і день став вільним, у базі не залишиться застарілих занять.
    Повертає кількість збережених занять.
    """
    count = 0
    with get_connection() as conn:
        conn.execute("DELETE FROM lessons WHERE group_id = ? AND lesson_date BETWEEN ? AND ?",
                     (group_id, str(start), str(end)))
        for day in data["schedule"]:
            for sub in day["subjects"]:
                conn.execute("""
                    INSERT INTO lessons (group_id, lesson_date, start_time, end_time,
                                         subject, type, teacher, audience, subgroup, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (group_id, day["date"], sub["time"], sub.get("end_time"), sub["name"],
                      sub.get("type"), sub.get("teacher"), sub.get("audience"),
                      sub.get("subgroup"), source))
                count += 1
    return count


def get_lessons(group_id, lesson_date: date):
    """Пари групи на конкретну дату, впорядковані за часом."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM lessons
            WHERE group_id = ? AND lesson_date = ?
            ORDER BY start_time, subgroup
        """, (group_id, str(lesson_date))).fetchall()


def get_lessons_range(group_id, start: date, end: date):
    """Пари групи за період [start; end], впорядковані за датою і часом."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM lessons
            WHERE group_id = ? AND lesson_date BETWEEN ? AND ?
            ORDER BY lesson_date, start_time, subgroup
        """, (group_id, str(start), str(end))).fetchall()


def get_next_lesson_date(group_id, subject, after_date: date):
    """Дата наступної пари з предмета після вказаної дати (знадобиться для ДЗ)."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT MIN(lesson_date) AS d FROM lessons
            WHERE group_id = ? AND subject = ? AND lesson_date > ?
        """, (group_id, subject, str(after_date))).fetchone()
        return row["d"]


# ---------- Домашнє завдання ----------

def add_homework(group_id, subject, due_date, text, created_by,
                 owner_user_id=None, status="active", replaces_id=None, attachments=()):
    """Додає завдання разом із вкладеннями [(file_id, file_type), ...]. Повертає id."""
    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO homework (group_id, subject, due_date, text, created_by,
                                  owner_user_id, status, replaces_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (group_id, subject, str(due_date), text, created_by,
              owner_user_id, status, replaces_id))
        hw_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO homework_attachments (homework_id, file_id, file_type) VALUES (?, ?, ?)",
            [(hw_id, file_id, file_type) for file_id, file_type in attachments])
    return hw_id


def get_homework_by_id(hw_id):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM homework WHERE id = ?", (hw_id,)).fetchone()


def get_attachments(hw_id):
    with get_connection() as conn:
        return conn.execute(
            "SELECT file_id, file_type FROM homework_attachments WHERE homework_id = ? ORDER BY id",
            (hw_id,)).fetchall()


def find_active_group_homework(group_id, subject, due_date):
    """Чинне групове завдання з предмета на дату (для перевірки конфлікту)."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM homework
            WHERE group_id = ? AND subject = ? AND due_date = ?
              AND owner_user_id IS NULL AND status = 'active'
            ORDER BY id DESC LIMIT 1
        """, (group_id, subject, str(due_date))).fetchone()


def resolve_pending_homework(hw_id, approve: bool) -> bool:
    """
    Рішення адміністратора щодо заміни завдання.
    Умова status = 'pending' в UPDATE гарантує, що спрацює лише перше натискання,
    навіть якщо два адміністратори натиснуть кнопку одночасно.
    """
    with get_connection() as conn:
        cur = conn.execute("UPDATE homework SET status = ? WHERE id = ? AND status = 'pending'",
                           ("active" if approve else "rejected", hw_id))
        if cur.rowcount == 0:
            return False
        if approve:
            conn.execute("""
                UPDATE homework SET status = 'replaced'
                WHERE id = (SELECT replaces_id FROM homework WHERE id = ?) AND status = 'active'
            """, (hw_id,))
    return True


def get_homework(group_id, due_date: date, user_id=None):
    """Чинні завдання групи на дату; якщо вказано user_id, то й особисті завдання студента."""
    return get_homework_range(group_id, due_date, due_date, user_id)


def get_homework_range(group_id, start: date, end: date, user_id=None):
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM homework
            WHERE group_id = ? AND status = 'active' AND due_date BETWEEN ? AND ?
              AND (owner_user_id IS NULL OR owner_user_id = ?)
            ORDER BY due_date, subject
        """, (group_id, str(start), str(end), user_id)).fetchall()


def get_group_subjects(group_id, start: date, end: date):
    """Перелік предметів групи з розкладу за період (для кнопок вибору)."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT DISTINCT subject FROM lessons
            WHERE group_id = ? AND lesson_date BETWEEN ? AND ?
            ORDER BY subject
        """, (group_id, str(start), str(end))).fetchall()
        return [r["subject"] for r in rows]


def get_group_chats(group_id):
    with get_connection() as conn:
        return [r["chat_id"] for r in conn.execute(
            "SELECT chat_id FROM group_chats WHERE group_id = ?", (group_id,))]


def get_personal_homework_on(due_date: date):
    """Чинні особисті завдання всіх студентів на дату (для нагадувань у ЛС)."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM homework
            WHERE owner_user_id IS NOT NULL AND status = 'active' AND due_date = ?
            ORDER BY owner_user_id, subject
        """, (str(due_date),)).fetchall()