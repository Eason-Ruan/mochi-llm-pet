"""Chinese display times; domain UTC timestamps remain unchanged."""
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')


def local_time(value):
    time = datetime.fromisoformat(value) if isinstance(value, str) else value
    if time.tzinfo is None:
        raise ValueError('display time must include timezone')
    return time.astimezone(ZONE)


def format_time(value, reference=None):
    time = local_time(value)
    day = f'{time.year}年{time.month}月{time.day}日'
    if reference is not None:
        delta = (time.date() - local_time(reference).date()).days
        day = {0:'今天', 1:'明天', -1:'昨天'}.get(delta, day)
    clock = time.strftime('%H:%M:%S' if time.second else '%H:%M')
    return f'{day} {clock}（北京时间 UTC+08:00）'
