from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import schedule
from app.schedule import Cron, Window, parse_field

UTC = timezone.utc


def dt(*a, tz=UTC):
    return datetime(*a, tzinfo=tz)


@pytest.mark.parametrize("text,lo,hi,names,expected", [
    ("*", 0, 5, {}, {0, 1, 2, 3, 4, 5}),
    ("*/2", 0, 5, {}, {0, 2, 4}),
    ("1-3", 0, 5, {}, {1, 2, 3}),
    ("1-5/2", 0, 5, {}, {1, 3, 5}),
    ("2/2", 0, 6, {}, {2, 4, 6}),
    ("1,4", 0, 5, {}, {1, 4}),
    ("mon-wed", 0, 7, schedule.DAYS, {1, 2, 3}),
    ("JAN,dec", 1, 12, schedule.MONTHS, {1, 12}),
])
def test_parse_field(text, lo, hi, names, expected):
    assert parse_field(text, lo, hi, names) == expected


@pytest.mark.parametrize("expr,msg", [
    ("* * * *", "5 fields"),
    ("60 * * * *", "not in 0-59"),
    ("* * * * funday", "not in 0-7"),
    ("5-1 * * * *", "bad range"),
    ("*/0 * * * *", "bad step"),
    ("0 0 31 2 *", "never matches"),
])
def test_invalid(expr, msg):
    with pytest.raises(ValueError, match=msg):
        Cron(expr).next(dt(2026, 1, 1))


def test_daily_and_strictly_after():
    c = Cron("30 4 * * *")
    assert c.next(dt(2026, 11, 21, 3, 0)) == dt(2026, 11, 21, 4, 30)
    assert c.next(dt(2026, 11, 21, 4, 30)) == dt(2026, 11, 22, 4, 30)
    assert c.next(dt(2026, 11, 21, 4, 29, 59)) == dt(2026, 11, 21, 4, 30)


def test_aliases_and_str():
    assert Cron("@hourly").next(dt(2026, 1, 1, 10, 15)) == dt(2026, 1, 1, 11, 0)
    assert Cron("@weekly").next(dt(2026, 11, 21)) == dt(2026, 11, 22)  # Sunday
    assert Cron("@monthly").next(dt(2026, 11, 21)) == dt(2026, 12, 1)
    assert str(Cron(" @daily ")) == "@daily"


def test_weekdays_and_sunday_as_7():
    c = Cron("0 3 * * 1-5")
    assert c.next(dt(2026, 11, 20, 4)) == dt(2026, 11, 23, 3)  # Fri after 3am -> Mon
    assert Cron("0 0 * * 7").next(dt(2026, 11, 21)) == dt(2026, 11, 22)


def test_dom_dow_or_semantics():
    c = Cron("0 0 1 * mon")  # 1st of month OR Mondays
    assert c.next(dt(2026, 11, 21)) == dt(2026, 11, 23)
    assert c.next(dt(2026, 11, 30, 1)) == dt(2026, 12, 1)


def test_month_restriction_and_leap_day():
    assert Cron("0 0 29 2 *").next(dt(2026, 3, 1)) == dt(2028, 2, 29)
    assert Cron("15 */6 * jun *").next(dt(2026, 11, 21)) == dt(2027, 6, 1, 0, 15)


def test_timezone_preserved():
    tz = ZoneInfo("America/Costa_Rica")
    n = Cron("0 4 * * *").next(datetime(2026, 11, 21, 6, 0, tzinfo=tz))
    assert n.tzinfo is tz and (n.day, n.hour) == (22, 4)


def test_month_rollover():
    assert Cron("0 0 * * *").next(dt(2026, 12, 31, 12)) == dt(2027, 1, 1)


@pytest.mark.parametrize("text,inside,outside", [
    ("02:00-05:00", [(2, 0), (4, 59)], [(1, 59), (5, 0), (12, 0)]),
    ("22:30-03:00", [(23, 0), (0, 0), (2, 59), (22, 30)], [(3, 0), (12, 0), (22, 29)]),
    ("00:00-24:00", [(0, 0), (23, 59)], []),
])
def test_window(text, inside, outside):
    w = Window(text)
    assert str(w) == text
    for h, m in inside:
        assert w.contains(dt(2026, 1, 1, h, m)), (h, m)
    for h, m in outside:
        assert not w.contains(dt(2026, 1, 1, h, m)), (h, m)


@pytest.mark.parametrize("text", ["2-5", "25:00-26:00", "02:00", "aa:bb-cc:dd", "24:30-01:00"])
def test_window_invalid(text):
    with pytest.raises(ValueError, match="expected HH:MM-HH:MM"):
        Window(text)
