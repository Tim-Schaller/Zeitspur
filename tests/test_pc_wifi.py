"""Ereignisprotokolle: XML lesen, PC-Zeiten (Start, Standby, Herunterfahren) und WLAN-Verbindungen."""
import json

import pytest

from zeitspur import eventlog, pc_times, wifi
from zeitspur.eventlog import LogEvent

H = 3_600_000
T = 1_790_000_000_000


def test_systemzeit_mit_sieben_nachkommastellen():
    assert eventlog.parse_systemtime("2026-10-04T05:52:10.1234567Z") == \
        eventlog.parse_systemtime("2026-10-04T05:52:10Z") + 123
    with pytest.raises(ValueError):
        eventlog.parse_systemtime("gestern")


def test_ereignis_xml():
    xml = ("<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
           "<Provider Name='Microsoft-Windows-Kernel-General' Guid='{a}'/><EventID>12</EventID>"
           "<TimeCreated SystemTime='2026-10-04T05:52:11.5000000Z'/><Channel>System</Channel></System>"
           "<EventData><Data Name='MajorVersion'>10</Data><Data Name='StartTime'>2026-10-04T05:52:10.0000000Z</Data>"
           "</EventData></Event>")
    ev = eventlog.parse_event_xml(xml)
    assert ev.event_id == 12 and ev.provider == "Microsoft-Windows-Kernel-General"
    assert ev.ts_ms == eventlog.parse_systemtime("2026-10-04T05:52:11.5Z")
    assert ev.data["StartTime"].startswith("2026-10-04T05:52:10")
    assert eventlog.parse_event_xml("<kaputt") is None


def test_abfrage_filtert_ids_und_zeitraum():
    xp = eventlog.build_xpath([12, 13], 0, 1000)
    assert "EventID=12 or EventID=13" in xp and "1970-01-01T00:00:00.000Z" in xp and "1970-01-01T00:00:01.000Z" in xp


# --------------------------------------------------------------------------- PC-Zeiten

def _ev(provider, eid, ts, **data):
    return LogEvent(eid, provider, ts, data)


G, P = pc_times.GENERAL, pc_times.POWER


def test_start_standby_und_herunterfahren():
    events = [_ev(G, 12, T), _ev(P, 506, T + 4 * H), _ev(P, 507, T + 5 * H),
              _ev(P, 506, T + 5 * H + 30_000),                       # kurzes Aufwachen im Standby faellt weg
              _ev(P, 507, T + 6 * H), _ev(G, 13, T + 9 * H, StopTime=eventlog.iso_utc(T + 9 * H))]
    rows = pc_times.periods(events, now_ms=T + 20 * H)
    assert [(r["ts_start"], r["ts_end"], r["subject"]) for r in rows] == [
        (T, T + 4 * H, "PC an (Start → Standby)"),
        (T + 6 * H, T + 9 * H, "PC an (Standby beendet → Herunterfahren)")]
    assert all(r["category"] == "pc_on" for r in rows)


def test_absturz_und_ruhezustand_mit_aufwachzeiten():
    events = [_ev(G, 12, T), _ev(P, 506, T + H), _ev(P, 507, T + 2 * H),
              _ev(G, 12, T + 3 * H),                                  # Neustart ohne Herunterfahren
              _ev(pc_times.TROUBLESHOOTER, 1, T + 8 * H, SleepTime=eventlog.iso_utc(T + 5 * H),
                  WakeTime=eventlog.iso_utc(T + 7 * H))]
    rows = pc_times.periods(events, now_ms=T + 9 * H)
    assert [(r["ts_start"], r["ts_end"]) for r in rows] == [(T, T + H), (T + 3 * H, T + 5 * H), (T + 7 * H, T + 9 * H)]
    assert rows[1]["subject"] == "PC an (Start → Ruhezustand)"
    assert rows[2]["subject"] == "PC an (seit Aufwachen)" and json.loads(rows[2]["extra"])["in_progress"]


# --------------------------------------------------------------------------- WLAN

IF = "{4711}"


def _con(ts, cid, ssid="Firma-WLAN"):
    return LogEvent(8001, "Microsoft-Windows-WLAN-AutoConfig", ts,
                    {"InterfaceGuid": IF, "ConnectionId": cid, "SSID": ssid, "ProfileName": ssid})


def _dis(ts, cid, ssid="Firma-WLAN"):
    return LogEvent(8003, "Microsoft-Windows-WLAN-AutoConfig", ts,
                    {"InterfaceGuid": IF, "ConnectionId": cid, "SSID": ssid})


def test_wlan_verbindungen_mit_orten():
    events = [_con(T, "1"), _dis(T + H, "1"),
              _con(T + H + 60_000, "2"), _dis(T + 2 * H, "2"),        # kurze Unterbrechung: eine Verbindung
              _con(T + 3 * H, "3", "Zuhause-WLAN")]                    # ohne Trennung, PC heruntergefahren
    rows = wifi.connections(events, stops=[T + 5 * H], now_ms=T + 10 * H, places=wifi.parse_places(
        ["Firma-WLAN = Büro"]))
    assert [(r["ts_start"], r["ts_end"], r["subject"]) for r in rows] == [
        (T, T + 2 * H, "Büro (WLAN Firma-WLAN)"),
        (T + 3 * H, T + 5 * H, "WLAN: Zuhause-WLAN")]
    assert json.loads(rows[0]["extra"])["place"] == "Büro" and rows[0]["category"] == "wifi"
    assert not json.loads(rows[1]["extra"])["in_progress"]


def test_laufende_wlan_verbindung():
    [row] = wifi.connections([_con(T, "1")], stops=[], now_ms=T + H)
    assert row["ts_end"] == T + H and json.loads(row["extra"])["in_progress"]


def test_orte_zuordnung_pruefen():
    assert wifi.parse_places(["  Firma = Büro ", "", "heim=Zuhause"]) == {"firma": "Büro", "heim": "Zuhause"}
    with pytest.raises(ValueError, match="Zeile 1"):
        wifi.parse_places(["nur ein Name"])
