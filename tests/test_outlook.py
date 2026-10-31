"""Outlook-Kalender-Abbildung (reine, COM-freie Teile)."""
import json
from datetime import datetime

import pytest

from zeitspur import timeutil
from zeitspur.outlook import (MK_E_UNAVAILABLE, OutlookCalendar, OutlookError, OutlookNotRunning,
                              _com_datetime_to_ms, build_event, connect)

START = timeutil.to_ms(datetime(2026, 9, 9, 12, 0, 0))
END = timeutil.to_ms(datetime(2026, 9, 9, 12, 30, 0))


def test_build_event_meeting():
    e = build_event(subject="  Serverumzug  ", start_ms=START, end_ms=END, location="Raum B2",
                    organizer="Anna Müller", attendees=["Anna Müller", "Max Mustermann", ""],
                    entry_id="AAABBB", meeting_status=1, busy_status=2, all_day=False)
    row = e.as_row()
    assert row["subject"] == "Serverumzug" and row["category"] == "meeting"
    assert row["ext_id"] == "AAABBB" and row["ts_start"] == START and row["ts_end"] == END
    assert row["organizer"] == "Anna Müller" and row["location"] == "Raum B2"
    assert row["attendees"] == "Anna Müller; Max Mustermann"  # leere Namen entfernt
    extra = json.loads(row["extra"])
    assert extra["meeting_status"] == 1 and extra["busy_status"] == 2 and extra["all_day"] is False


def test_build_event_appointment_and_attendee_string():
    e = build_event(subject="Fokuszeit", start_ms=START, end_ms=START, attendees="nur ich", meeting_status=0)
    assert e.category == "appointment" and e.attendees == "nur ich"
    assert e.ts_end == e.ts_start  # end < start wird auf start angehoben


def test_build_event_canceled_meeting():
    e = build_event(subject="Abgesagt", start_ms=START, end_ms=END, meeting_status=5)
    assert e.category == "meeting" and json.loads(e.extra)["canceled"] is True


def test_com_datetime_to_ms():
    assert _com_datetime_to_ms(None) is None
    assert _com_datetime_to_ms(datetime(2026, 9, 9, 12, 0, 0)) == START

    class FakeComTime:  # pywintypes.datetime-artig
        def timestamp(self):
            return START / 1000

    assert _com_datetime_to_ms(FakeComTime()) == START


def test_available_returns_bool():
    assert isinstance(OutlookCalendar.available(), bool)


class FakeComError(Exception):  # pywintypes.com_error-artig
    def __init__(self, hresult):
        super().__init__(hresult, "Fehler", None, None)
        self.hresult = hresult


class FakeComClient:
    """win32com.client-artig: merkt sich, ob Outlook gestartet oder nur angehaengt wurde."""

    def __init__(self, running=True, error=None):
        self.running = running
        self.error = error
        self.calls = []

    def GetActiveObject(self, progid):
        self.calls.append(("attach", progid))
        if self.error is not None:
            raise self.error
        if not self.running:
            raise FakeComError(MK_E_UNAVAILABLE)
        return "laufendes Outlook"

    def Dispatch(self, progid):
        self.calls.append(("launch", progid))
        return "gestartetes Outlook"


def test_connect_haengt_sich_nur_an_laufendes_outlook():
    client = FakeComClient(running=True)
    assert connect(client) == "laufendes Outlook"
    assert client.calls == [("attach", "Outlook.Application")]   # nie Dispatch -> nie ein Start


def test_connect_startet_outlook_nicht_wenn_es_nicht_laeuft():
    client = FakeComClient(running=False)
    with pytest.raises(OutlookNotRunning):
        connect(client)
    assert [c[0] for c in client.calls] == ["attach"]


def test_connect_startet_outlook_nur_auf_wunsch():
    client = FakeComClient(running=False)
    assert connect(client, launch=True) == "gestartetes Outlook"
    assert client.calls == [("launch", "Outlook.Application")]


def test_connect_reicht_andere_com_fehler_weiter():
    client = FakeComClient(error=FakeComError(-2147467259))   # E_FAIL ist kein "laeuft nicht"
    with pytest.raises(FakeComError):
        connect(client)


def test_outlook_not_running_ist_ein_outlook_error():
    # Aufrufer, die nur OutlookError kennen, behalten so ihren Zwischenspeicher
    assert issubclass(OutlookNotRunning, OutlookError)
