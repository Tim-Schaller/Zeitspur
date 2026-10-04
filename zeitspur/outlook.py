"""Outlook-Kalender lokal ueber COM lesen (klassisches Outlook / Outlook Object Model).

Nutzt den bereits angemeldeten Outlook-Desktop-Client, es ist keine zusaetzliche Authentifizierung noetig.
Alle Zugriffe sind lesend. Faellt COM aus (kein klassisches Outlook, kein Profil), liefert fetch() eine
leere Liste und available() False - Zeitspur laeuft dann ohne Kalender weiter.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import timeutil

log = logging.getLogger(__name__)

SOURCE = "outlook"
OL_FOLDER_CALENDAR = 9
# Puffer fuer die untere Restrict-Grenze: faengt Termine, die bis zu so viele Tage vor dem Fenster
# begannen und in dieses hineinreichen (mehrtaegige/ganztaegige Termine).
LONG_EVENT_BUFFER_DAYS = 62
# MeetingStatus: 0=olNonMeeting, 1=olMeeting, 3=olMeetingReceived, 5=olMeetingCanceled, 7=olMeetingReceivedAndCanceled
MEETING_STATUSES = {1, 3, 5, 7}
CANCELED_STATUSES = {5, 7}


@dataclass
class CalendarEvent:
    ext_id: str | None
    ts_start: int
    ts_end: int
    subject: str
    location: str | None
    organizer: str | None
    attendees: str | None
    category: str
    extra: str | None

    def as_row(self) -> dict:
        return self.__dict__.copy()


def _com_datetime_to_ms(value) -> int | None:
    """pywintypes/COM-Datetime -> Unix-ms. COM liefert i. d. R. zeitzonenbewusste Werte (lokale Zeit)."""
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            return timeutil.to_ms(value)
        # pywintypes.datetime unterstuetzt timestamp(); Fallback ueber ISO-Parsing
        return int(round(value.timestamp() * 1000))
    except Exception:
        try:
            return timeutil.to_ms(datetime.fromisoformat(str(value)))
        except Exception:
            return None


def build_event(*, subject: str, start_ms: int, end_ms: int, location: str | None = None,
                organizer: str | None = None, attendees: list[str] | str | None = None,
                entry_id: str | None = None, meeting_status: int = 0, busy_status: int | None = None,
                all_day: bool = False) -> CalendarEvent:
    """Reine Abbildung eines Outlook-Termins auf unser Ereignis-Schema (ohne COM, daher testbar)."""
    if isinstance(attendees, (list, tuple)):
        att = "; ".join(a for a in (str(x).strip() for x in attendees) if a) or None
    else:
        att = (attendees or None)
    is_meeting = meeting_status in MEETING_STATUSES
    category = "meeting" if is_meeting else "appointment"
    extra = {"meeting_status": meeting_status, "all_day": bool(all_day)}
    if busy_status is not None:
        extra["busy_status"] = busy_status
    if meeting_status in CANCELED_STATUSES:
        extra["canceled"] = True
    return CalendarEvent(
        ext_id=str(entry_id) if entry_id else None,
        ts_start=int(start_ms), ts_end=max(int(end_ms), int(start_ms)),
        subject=(subject or "").strip(), location=(location or None), organizer=(organizer or None),
        attendees=att, category=category, extra=json.dumps(extra, ensure_ascii=False))


class OutlookError(Exception):
    pass


class OutlookCalendar:
    """Liest Termine aus dem klassischen Outlook. Instanzen sind an einen Thread gebunden (COM-Apartment)."""

    def __init__(self, max_attendees: int = 25):
        self.max_attendees = max_attendees
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @staticmethod
    def available() -> bool:
        """True, wenn das klassische Outlook-COM registriert ist (ohne Outlook zu starten)."""
        if sys.platform != "win32":
            return False
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Outlook.Application\CLSID"):
                return True
        except OSError:
            return False

    def fetch(self, start: date, end: date) -> list[CalendarEvent]:
        """Termine, die den Tagesbereich [start, end] (lokal, end inklusive) ueberlappen.

        Leere Liste heisst "keine Termine". Bei einem COM-/Zugriffsfehler wird eine OutlookError GEWORFEN -
        das ist wichtig, weil der Aufrufer sonst eine leere Liste als "keine Termine" interpretiert und ueber
        replace_events die zwischengespeicherten Termine loeschen wuerde.
        """
        self._last_error = None
        if sys.platform != "win32":
            return []
        try:
            import pythoncom
            import win32com.client
        except ImportError as e:  # pragma: no cover
            self._last_error = f"pywin32 fehlt: {e}"
            raise OutlookError(self._last_error) from e

        window_start = datetime(start.year, start.month, start.day)
        window_end = datetime(end.year, end.month, end.day) + timedelta(days=1)
        # Untere Restrict-Grenze puffern, damit Termine, die VOR dem Fenster begannen und hineinreichen
        # (z. B. mehrtaegige/ganztaegige Termine), nicht ausgelassen - und damit von replace_events geloescht -
        # werden. Der praezise Ueberlappungsfilter sitzt anschliessend in _collect.
        restrict_start = window_start - timedelta(days=LONG_EVENT_BUFFER_DAYS)
        pythoncom.CoInitialize()
        try:
            app = win32com.client.Dispatch("Outlook.Application")
            namespace = app.GetNamespace("MAPI")
            calendar = namespace.GetDefaultFolder(OL_FOLDER_CALENDAR)
            items = calendar.Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            restriction = ("[Start] >= '%s' AND [Start] < '%s'"
                           % (restrict_start.strftime("%m/%d/%Y %I:%M %p"),
                              window_end.strftime("%m/%d/%Y %I:%M %p")))
            try:
                restricted = items.Restrict(restriction)
            except Exception as e:
                # Restrict ist bei manchen Outlook-/Locale-Konstellationen zickig -> manuell filtern
                log.debug("Outlook.Restrict fehlgeschlagen (%s), Fallback ohne Wiederholungen", e)
                items.IncludeRecurrences = False
                restricted = items
            events = self._collect(restricted, timeutil.to_ms(window_start), timeutil.to_ms(window_end))
            log.info("Outlook: %d Termine fuer %s..%s gelesen", len(events), start, end)
            return events
        except Exception as e:  # pywintypes.com_error u. a. -> WERFEN, nicht [] (siehe Docstring)
            self._last_error = str(e)
            log.warning("Outlook-Kalender konnte nicht gelesen werden: %s", e)
            raise OutlookError(self._last_error) from e
        finally:
            pythoncom.CoUninitialize()

    def _collect(self, restricted, window_start_ms: int, window_end_ms: int) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        count = 0
        for item in restricted:
            count += 1
            if count > 2000:  # Sicherheitsnetz gegen endlose Wiederholungsserien
                log.warning("Outlook: Terminliste abgeschnitten (>2000)")
                break
            try:
                start_ms = _com_datetime_to_ms(item.Start)
                end_ms = _com_datetime_to_ms(item.End)
                if start_ms is None or end_ms is None:
                    continue
                if start_ms >= window_end_ms or end_ms < window_start_ms:
                    continue
                events.append(self._event_from_item(item, start_ms, end_ms))
            except Exception as e:  # einzelner defekter Termin darf den Rest nicht kippen
                log.debug("Outlook-Termin uebersprungen: %s", e)
        events.sort(key=lambda e: e.ts_start)
        return events

    def _event_from_item(self, item, start_ms: int, end_ms: int) -> CalendarEvent:
        def attr(name, default=None):
            try:
                return getattr(item, name)
            except Exception:
                return default

        attendees: list[str] = []
        try:
            recipients = item.Recipients
            for i in range(1, recipients.Count + 1):
                if len(attendees) >= self.max_attendees:
                    break
                name = recipients.Item(i).Name
                if name:
                    attendees.append(str(name))
        except Exception:
            pass
        return build_event(
            subject=attr("Subject", "") or "",
            start_ms=start_ms, end_ms=end_ms,
            location=attr("Location"),
            organizer=attr("Organizer"),
            attendees=attendees or None,
            entry_id=attr("EntryID"),
            meeting_status=int(attr("MeetingStatus", 0) or 0),
            busy_status=attr("BusyStatus"),
            all_day=bool(attr("AllDayEvent", False)),
        )
