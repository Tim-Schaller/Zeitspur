"""Kalender per ICS-Link: Links pruefen, Termine samt Serien und Zeitzonen lesen, Fehler ohne geheimen Link."""
import json
from datetime import date, datetime, timezone

import pytest

from zeitspur import httpclient
from zeitspur import ics_calendar as ics
from zeitspur.plugins import PluginError

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//DE
X-WR-CALNAME:Arbeit
BEGIN:VEVENT
UID:termin-1@test
DTSTART;TZID=Europe/Berlin:20261005T090000
DTEND;TZID=Europe/Berlin:20261005T100000
SUMMARY:Projektbesprechung
LOCATION:Raum 1
ORGANIZER;CN=Max Mustermann:mailto:max@beispiel.de
ATTENDEE;CN=Erika Musterfrau:mailto:erika@beispiel.de
END:VEVENT
BEGIN:VEVENT
UID:serie@test
DTSTART;TZID=Europe/Berlin:20261001T080000
DTEND;TZID=Europe/Berlin:20261001T081500
RRULE:FREQ=DAILY;COUNT=10
EXDATE;TZID=Europe/Berlin:20261003T080000
SUMMARY:Daily
END:VEVENT
BEGIN:VEVENT
UID:ganztag@test
DTSTART;VALUE=DATE:20261006
DTEND;VALUE=DATE:20261007
SUMMARY:Urlaub
END:VEVENT
BEGIN:VEVENT
UID:abgesagt@test
DTSTART:20261005T120000Z
DTEND:20261005T130000Z
STATUS:CANCELLED
SUMMARY:Faellt aus
END:VEVENT
END:VCALENDAR
"""

GOOGLE = b"""BEGIN:VCALENDAR
VERSION:2.0
X-WR-TIMEZONE:Europe/Berlin
BEGIN:VEVENT
UID:g@test
DTSTART:20261005T090000
DTEND:20261005T093000
SUMMARY:Schwebend
END:VEVENT
END:VCALENDAR
"""


def _utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def test_links_pruefen():
    assert ics.parse_lines("Arbeit | webcal://kal.example.org/a.ics\n\nhttps://b.example.org/b.ics") == [
        ("Arbeit", "https://kal.example.org/a.ics"), (None, "https://b.example.org/b.ics")]
    with pytest.raises(ValueError, match="Zeile 2") as err:
        ics.parse_lines("https://ok.example.org/a.ics\nhttp://geheim-123.example.org/b.ics")
    assert "geheim" not in str(err.value)
    with pytest.raises(ValueError, match="mindestens"):
        ics.parse_lines("  \n")


def test_termine_serien_ganztag_und_absagen():
    name, rows = ics.parse_calendar(ICS, date(2026, 10, 1), date(2026, 10, 6), "Ersatz")
    assert name == "Arbeit"
    by_subject = {}
    for r in rows:
        by_subject.setdefault(r["subject"], []).append(r)
    assert sorted(by_subject) == ["Daily", "Projektbesprechung", "Urlaub"]          # Absage fehlt
    assert len(by_subject["Daily"]) == 5                                              # 1.-6.10. ohne den 3.
    [meeting] = by_subject["Projektbesprechung"]
    assert meeting["ts_start"] == _utc(2026, 10, 5, 7, 0) and meeting["ts_end"] == _utc(2026, 10, 5, 8, 0)
    assert meeting["organizer"] == "Max Mustermann" and meeting["attendees"] == "Erika Musterfrau"
    assert meeting["category"] == "meeting" and meeting["location"] == "Raum 1"
    [urlaub] = by_subject["Urlaub"]
    assert urlaub["ts_start"] == int(datetime(2026, 10, 6).timestamp() * 1000)
    assert urlaub["ts_end"] == int(datetime(2026, 10, 7).timestamp() * 1000) - 1
    assert json.loads(urlaub["extra"]) == {"calendar": "Arbeit", "all_day": True}
    assert len({r["ext_id"] for r in rows}) == len(rows)                              # jede Serie einzeln


def test_google_zeitzone_im_kalenderkopf():
    _, [row] = ics.parse_calendar(GOOGLE, date(2026, 10, 5), date(2026, 10, 5), "Google")
    assert row["ts_start"] == _utc(2026, 10, 5, 7, 0)


def test_mehrere_kalender_eigene_namen_und_fehler_ohne_link():
    def load(url):
        if "kaputt" in url:
            raise httpclient.HttpError("Zugriff verweigert (HTTP 401)", 401)
        return ICS
    rows = ics.fetch_all("Team | https://a.example.org/x.ics", date(2026, 10, 5), date(2026, 10, 5), load=load)
    assert {json.loads(r["extra"])["calendar"] for r in rows} == {"Team"}
    with pytest.raises(PluginError, match="Privat") as err:
        ics.fetch_all("https://a.example.org/x.ics\nPrivat | https://kaputt-geheim.example.org/y.ics",
                      date(2026, 10, 5), date(2026, 10, 5), load=load)
    assert "geheim" not in str(err.value)
    with pytest.raises(PluginError, match="keine gültige Kalenderdatei"):
        ics.fetch_all("https://a.example.org/x.ics", date(2026, 10, 5), date(2026, 10, 5), load=lambda u: b"<html>")


def test_verbindung_testen_zaehlt_termine():
    text = ics.test_calendars("https://a.example.org/x.ics", load=lambda url: ICS)
    assert text.startswith("Erreichbar: Arbeit (")
