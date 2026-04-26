import sqlite3

DB_NAME = "database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''  
        CREATE TABLE IF NOT EXISTS lessons
        (
            id
            INTEGER
            NOT
            NULL
            PRIMARY
            KEY
            AUTOINCREMENT,
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

def save_lessons(data):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM lessons")
    for day_item in data['schedule']:
        day_name = day_item['day']
        print(day_name)
        for sub in day_item['subjects']:
            cursor.execute('''
            INSERT INTO lessons (day, subject_name, start_time, type, teacher, audience)
                           VALUES (?, ?, ?, ?, ?, ?)''', (
                day_name,
                sub['name'],
                sub['time'],
                sub['type'],
                sub['teacher'],
                sub['audience']
            ))
    conn.commit()
    conn.close()

