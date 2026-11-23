"""Chinese display times; domain UTC timestamps remain unchanged."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ZONE_NAME = 'Asia/Shanghai'

try:
    ZONE = ZoneInfo(ZONE_NAME)
except ZoneInfoNotFoundError:
    ZONE = timezone(timedelta(hours=8), ZONE_NAME)


def local_time(value, *, zone=None):
    time = datetime.fromisoformat(value) if isinstance(value, str) else value
    if time.utcoffset() is None:
        raise ValueError('display time must include timezone')
    return time.astimezone(ZoneInfo(zone) if zone is not None else ZONE)


def format_time(value, reference=None, *, zone=None):
    time = local_time(value, zone=zone)
    day = f'{time.year}年{time.month}月{time.day}日'
    if reference is not None:
        delta = (time.date() - local_time(reference, zone=zone).date()).days
        day = {0:'今天', 1:'明天', -1:'昨天'}.get(delta, day)
    clock = time.strftime('%H:%M:%S' if time.second else '%H:%M')
    if zone is None:
        return f'{day} {clock}（北京时间 UTC+08:00）'
    offset = time.isoformat(timespec='seconds')[len('YYYY-MM-DDTHH:MM:SS'):]
    return f'{day} {clock}（{zone} UTC{offset}）'
