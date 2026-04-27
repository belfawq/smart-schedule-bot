import sqlite3

DB_NAME = "database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''  
        CREATE TABLE IF NOT EXISTS lessons
        (
            id_lesson
            INTEGER
            NOT
            NULL
            PRIMARY
            KEY
            AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            day
            TEXT,
            subject_name
            TEXT,
            start_time
            TEXT,
            type
            TEXT,
            teacher
            TEXT,
            audience
            TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_lessons(data,user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM lessons where user_id = ?", (user_id,))
    for day_item in data['schedule']:
        day_name = day_item['day']
        for sub in day_item['subjects']:
            cursor.execute('''
            INSERT INTO lessons (user_id, day, subject_name, start_time, type, teacher, audience)
                           VALUES (?, ?, ?, ?, ?, ?, ?)''', ( user_id,
                day_name,
                sub['name'],
                sub['time'],
                sub['type'],
                sub['teacher'],
                sub['audience']
            ))
    conn.commit()
    conn.close()

