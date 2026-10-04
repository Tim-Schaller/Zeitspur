"""Zeit-Hilfsfunktionen: alle Zeitstempel in der DB sind Unix-Millisekunden (UTC)."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

MS_PER_DAY = 86_400_000


def now_ms() -> int:
    return int(time.time() * 1000)


def to_ms(dt: datetime) -> int:
    """datetime -> Unix-ms. Naive Werte werden als lokale Zeit interpretiert."""
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive == lokale Systemzeit
    return int(dt.timestamp() * 1000)


def to_local(ms: int) -> datetime:
    """Unix-ms -> zeitzonenbewusste lokale datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()


def local_offset_ms(at_ms: int | None = None) -> int:
    """Offset der lokalen Zeitzone zu UTC in ms (zum Zeitpunkt at_ms)."""
    dt = to_local(at_ms if at_ms is not None else now_ms())
    off = dt.utcoffset() or timedelta(0)
    return int(off.total_seconds() * 1000)


def day_bounds(day: date) -> tuple[int, int]:
    """(start_ms, end_ms) eines lokalen Kalendertags, end exklusiv."""
    start = datetime(day.year, day.month, day.day).astimezone()
    end = (datetime(day.year, day.month, day.day) + timedelta(days=1)).astimezone()
    return to_ms(start), to_ms(end)


def local_date(ms: int) -> date:
    return to_local(ms).date()


def iso_local(ms: int) -> str:
    """Lokale ISO-8601-Darstellung mit Offset, sekundengenau."""
    return to_local(ms).isoformat(timespec="seconds")


def fmt_hm(ms: int) -> str:
    return to_local(ms).strftime("%H:%M")


def fmt_hms(ms: int) -> str:
    return to_local(ms).strftime("%H:%M:%S")


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(text: str) -> date:
    text = text.strip()
    if _DATE_RE.match(text):
        return date.fromisoformat(text)
    if text.lower() in ("heute", "today"):
        return date.today()
    if text.lower() in ("gestern", "yesterday"):
        return date.today() - timedelta(days=1)
    return parse_when(text).date()


def parse_when(text: str) -> datetime:
    """Akzeptiert ISO 8601 (mit/ohne Offset, 'T' oder Leerzeichen), 'YYYY-MM-DD HH:MM[:SS]',
    'YYYY-MM-DD' sowie 'TT.MM.JJJJ [HH:MM]'. Naive Angaben gelten als lokale Zeit.
    Liefert eine zeitzonenbewusste datetime."""
    s = text.strip()
    if not s:
        raise ValueError("leere Zeitangabe")
    if s.lower() in ("jetzt", "now"):
        return datetime.now().astimezone()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Zeitangabe nicht verstanden: {text!r} (erwartet z. B. 2026-09-10 12:00)")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def human_duration(ms: int) -> str:
    s = max(0, ms // 1000)
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min" if s == 0 else f"{m} min {s} s"
    h, m = divmod(m, 60)
    return f"{h} h {m} min"


def iso_to_ms(value: str | None) -> int | None:
    """ISO-8601-Zeitstempel -> Unix-ms (UTC). None/unparsbar -> None.

    Vertraegt das 'Z'-Suffix und ueberlange Sekundenbruchteile (Microsoft Graph liefert bis zu
    sieben Stellen, datetime.fromisoformat nimmt hoechstens sechs).
    """
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, _, tail = s.partition(".")
        frac = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                frac += ch
            else:
                rest = tail[i:]
                break
        s = f"{head}.{frac[:6]}{rest}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # ohne Zeitzone: als lokale Zeit deuten
    return int(round(dt.timestamp() * 1000))
