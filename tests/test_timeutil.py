from datetime import date, datetime, timezone

import pytest

from zeitspur import timeutil


def test_ms_roundtrip_local():
    dt = datetime(2026, 9, 10, 12, 0, 0)  # naiv = lokal
    ms = timeutil.to_ms(dt)
    back = timeutil.to_local(ms)
    assert back.hour == 12 and back.minute == 0 and back.tzinfo is not None
    assert timeutil.fmt_hm(ms) == "12:00"
    assert timeutil.iso_local(ms).startswith("2026-09-10T12:00:00")


def test_day_bounds_cover_24h():
    start, end = timeutil.day_bounds(date(2026, 9, 10))
    assert end - start in (23 * 3_600_000, 24 * 3_600_000, 25 * 3_600_000)  # DST-Tage
    assert timeutil.local_date(start) == date(2026, 9, 10)
    assert timeutil.local_date(end - 1) == date(2026, 9, 10)
    assert timeutil.local_date(end) == date(2026, 9, 11)


@pytest.mark.parametrize("text", [
    "2026-09-10 12:00", "2026-09-10T12:00", "2026-09-10 12:00:00", "10.09.2026 12:00",
])
def test_parse_when_local_variants(text):
    dt = timeutil.parse_when(text)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 9, 10, 12, 0)
    assert dt.tzinfo is not None


def test_parse_when_with_offset_and_z():
    dt = timeutil.parse_when("2026-09-10T10:00:00Z")
    assert dt.astimezone(timezone.utc).hour == 10
    dt2 = timeutil.parse_when("2026-09-10T12:00:00+02:00")
    assert dt2.astimezone(timezone.utc).hour == 10


def test_parse_when_invalid():
    with pytest.raises(ValueError):
        timeutil.parse_when("letzten Mittwoch")
    with pytest.raises(ValueError):
        timeutil.parse_when("")


def test_parse_date():
    assert timeutil.parse_date("2026-09-10") == date(2026, 9, 10)
    assert timeutil.parse_date("2026-09-10 08:15") == date(2026, 9, 10)
    assert timeutil.parse_date("heute") == date.today()


def test_human_duration():
    assert timeutil.human_duration(5_000) == "5 s"
    assert timeutil.human_duration(120_000) == "2 min"
    assert timeutil.human_duration(125_000) == "2 min 5 s"
    assert timeutil.human_duration(3_720_000) == "1 h 2 min"
