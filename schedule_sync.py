"""Синхронізація розкладу групи з АСУ «ВНЗ»: завантаження і збереження в базу."""
from datetime import date, timedelta

import database as db
from parser import ScheduleSourceError, fetch_schedule

SYNC_DAYS = 14  # на скільки днів уперед завантажувати розклад із сервера


async def sync_group(group_id: int) -> tuple[int, date, date]:
    """Завантажує розклад групи на SYNC_DAYS днів уперед і зберігає в базу."""
    group = db.get_group(group_id)
    if not group or not group["site_group_id"]:
        raise ScheduleSourceError("Групу ще не прив'язано до АСУ «ВНЗ» "
                                  "(староста може зробити це командою /site у чаті групи)")
    start = date.today()
    end = start + timedelta(days=SYNC_DAYS)
    data = await fetch_schedule(group["site_group_id"], start, end)
    count = db.replace_lessons_range(data, group_id, start, end, source="web")
    return count, start, end