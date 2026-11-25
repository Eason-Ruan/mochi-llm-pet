"""Localized display times; domain UTC timestamps remain unchanged."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from deskpet.i18n import tr

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


def format_clock(time):
    return time.strftime('%H:%M:%S' if time.second else '%H:%M')


def format_day_clock(time, reference_day=None):
    delta = (time.date() - reference_day).days if reference_day is not None else None
    nearby_day = {-1: ('昨天', 'Yesterday'), 0: ('今天', 'Today'), 1: ('明天', 'Tomorrow')}.get(delta)
    if nearby_day:
        day = tr(*nearby_day)
    else:
        day = tr('{year}年{month}月{day}日', '{year}-{month:02d}-{day:02d}').format(
            year=time.year, month=time.month, day=time.day,
        )
    return f'{day} {format_clock(time)}'


def format_timezone(time, *, zone=None):
    if zone is None:
        return tr('北京时间', 'Beijing time') + ' UTC+08:00'
    offset = time.isoformat(timespec='seconds')[len('YYYY-MM-DDTHH:MM:SS'):]
    return f'{zone} UTC{offset}'


def format_time(value, reference=None, *, zone=None):
    time = local_time(value, zone=zone)
    reference_day = local_time(reference, zone=zone).date() if reference is not None else None
    return tr('{time}（{zone}）', '{time} ({zone})').format(
        time=format_day_clock(time, reference_day), zone=format_timezone(time, zone=zone),
    )
