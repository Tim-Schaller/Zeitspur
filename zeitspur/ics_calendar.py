"""Kalender per ICS-Link: Google Kalender, iCloud, Outlook.com/neues Outlook, Nextcloud, Exchange-Veroeffentlichung
- jeder Dienst, der einen (geheimen) iCal-Link anbietet.

Kein Login: Der Link selbst ist der Schluessel. Er wird deshalb wie ein Passwort behandelt - DPAPI-geschuetzt
gespeichert, nur ueber https abgerufen (webcal:// wird zu https://) und nie in Protokollen oder Fehlermeldungen
genannt; dort steht hoechstens der Name des Kalenders oder der Host. Serientermine loest recurring_ical_events
auf, X-WR-TIMEZONE (Google) uebersetzt x_wr_timezone.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

from . import httpclient

log = logging.getLogger(__name__)

SOURCE = "ics"
MAX_BYTES = 20 * 1024 * 1024
MAX_ATTENDEES = 25
# Obergrenze der aufgeloesten Termine je Kalender und Abgleichsfenster: ein boesartiger/kaputter Feed mit
# dicht getakteter Wiederholung (Sub-Minuten) darf die Aufloesung und die DB nicht aufblaehen (DoS).
MAX_OCCURRENCES = 5000


def parse_lines(text: str) -> list[tuple[str | None, str]]:
    """'Arbeit | https://...' bzw. nur die Adresse, je Zeile -> [(Name oder None, normalisierte Adresse)].

    Wirft ValueError mit der Zeilennummer - nie mit der Adresse selbst (sie ist geheim)."""
    out: list[tuple[str | None, str]] = []
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        name, sep, url = line.rpartition("|")
        if not sep:
            name, url = "", line
        try:
            out.append((name.strip() or None, httpclient.normalize_url(url)))
        except ValueError:
            raise ValueError(f"Zeile {n}: keine gültige https- oder webcal-Adresse") from None
    if not out:
        raise ValueError("Bitte mindestens einen Kalender-Link eintragen.")
    return out


def _ms(value) -> int:
    """date (ganztaegig) oder datetime (mit Zeitzone oder 'schwebend' = Ortszeit) -> Unix-ms."""
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)     # naiv = lokale Zeit, sonst die angegebene Zone
    return int(datetime.combine(value, time.min).timestamp() * 1000)


def _person(value) -> str | None:
    if value is None:
        return None
    cn = getattr(value, "params", {}).get("CN")
    text = str(cn) if cn else str(value)
    text = text.removeprefix("mailto:").removeprefix("MAILTO:").strip()
    return text or None


def event_row(ev, calendar_name: str) -> dict | None:
    """Ein (bereits aufgeloestes) VEVENT -> Zeile fuer calendar_events. Abgesagte Termine fallen weg."""
    if str(ev.get("STATUS", "")).upper() == "CANCELLED":
        return None
    start_prop = ev.get("DTSTART")
    if start_prop is None:
        return None
    start = start_prop.dt
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    end_prop = ev.get("DTEND")
    if end_prop is not None:
        end = end_prop.dt
    elif ev.get("DURATION") is not None:
        end = start + ev.get("DURATION").dt
    else:
        end = start + timedelta(days=1) if all_day else start
    start_ms, end_ms = _ms(start), _ms(end)
    if all_day:
        end_ms -= 1   # bis Ende des letzten Tages, nicht bis 0:00 des Folgetags
    attendees_raw = ev.get("ATTENDEE")
    if attendees_raw is None:
        attendees_raw = []
    elif not isinstance(attendees_raw, list):
        attendees_raw = [attendees_raw]
    attendees = [p for p in (_person(a) for a in attendees_raw[:MAX_ATTENDEES]) if p]
    uid = str(ev.get("UID", "")) or f"{calendar_name}-{start_ms}"
    return {
        "ext_id": f"{uid}|{start_ms}", "ts_start": start_ms, "ts_end": max(end_ms, start_ms),
        "subject": str(ev.get("SUMMARY", "")).strip() or "(ohne Titel)",
        "location": str(ev.get("LOCATION", "")).strip() or None,
        "organizer": _person(ev.get("ORGANIZER")),
        "attendees": "; ".join(attendees) or None,
        "category": "meeting" if attendees else "appointment",
        "extra": json.dumps({"calendar": calendar_name, "all_day": all_day}, ensure_ascii=False),
    }


def parse_calendar(data: bytes, start: date, end: date, fallback_name: str) -> tuple[str, list[dict]]:
    """ICS-Inhalt -> (Kalendername, Termine, die den Tagesbereich [start, end] beruehren)."""
    import icalendar
    import recurring_ical_events
    import x_wr_timezone

    try:
        cal = x_wr_timezone.to_standard(icalendar.Calendar.from_ical(data))
    except Exception as e:  # kaputtes ICS
        raise ValueError(f"keine gültige Kalenderdatei ({type(e).__name__})") from None
    name = str(cal.get("X-WR-CALNAME", "")).strip() or fallback_name
    window_start = datetime.combine(start, time.min).astimezone()
    window_end = datetime.combine(end + timedelta(days=1), time.min).astimezone()
    rows = []
    for ev in recurring_ical_events.of(cal).between(window_start, window_end):
        if len(rows) >= MAX_OCCURRENCES:
            log.warning("ICS-Kalender %s: Terminlimit (%d) erreicht, weitere Termine verworfen",
                        name, MAX_OCCURRENCES)
            break
        try:
            row = event_row(ev, name)
        except Exception as e:  # ein defekter Termin darf den Rest nicht kippen
            log.debug("ICS-Termin uebersprungen: %s", e)
            continue
        if row is not None:
            rows.append(row)
    return name, rows


def _load(url: str) -> bytes:
    return httpclient.request(url, headers={"Accept": "text/calendar, */*"}, max_bytes=MAX_BYTES)


def fetch_all(text: str, start: date, end: date, load=_load) -> list[dict]:
    """Alle Kalender. Scheitert einer, scheitert der Abgleich (sonst loeschte er dessen gespeicherte Termine)."""
    from .plugins import PluginError

    rows: list[dict] = []
    for name, url in parse_lines(text):
        label = name or httpclient.host_of(url)
        try:
            cal_name, events = parse_calendar(load(url), start, end, label)
        except (httpclient.HttpError, ValueError) as e:
            raise PluginError(f"Kalender „{label}“: {e}") from None
        for row in events:
            if name:   # selbst vergebener Name hat Vorrang vor dem im Kalender
                extra = json.loads(row["extra"])
                extra["calendar"] = name
                row["extra"] = json.dumps(extra, ensure_ascii=False)
        log.info("ICS-Kalender %s: %d Termine", name or cal_name, len(events))
        rows.extend(events)
    return rows


def test_calendars(text: str, load=_load) -> str:
    from .plugins import PluginError

    today = date.today()
    teile = []
    for name, url in parse_lines(text):
        label = name or httpclient.host_of(url)
        try:
            cal_name, events = parse_calendar(load(url), today, today + timedelta(days=13), label)
        except (httpclient.HttpError, ValueError) as e:
            raise PluginError(f"Kalender „{label}“: {e}") from None
        teile.append(f"{name or cal_name} ({len(events)} Termine in den nächsten 14 Tagen)")
    return "Erreichbar: " + ", ".join(teile) + "."
