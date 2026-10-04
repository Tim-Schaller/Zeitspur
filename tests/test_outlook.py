"""Outlook-Kalender-Abbildung (reine, COM-freie Teile)."""
import json
from datetime import datetime

from zeitspur import timeutil
from zeitspur.outlook import OutlookCalendar, _com_datetime_to_ms, build_event

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
