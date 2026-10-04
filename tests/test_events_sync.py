"""Synchronisierung der Ereignis-Plugins in die Datenbank (mit Fake-Quellen)."""
import threading
import time
from datetime import date, datetime, timedelta

import pytest

from zeitspur import plugins, timeutil
from zeitspur.config import Config
from zeitspur.events_sync import EventSync, EventSyncThread, SyncReport
from zeitspur.outlook import CalendarEvent, OutlookCalendar

DAY = date(2026, 9, 9)
T = lambda h, m=0: timeutil.to_ms(datetime(2026, 9, 9, h, m))  # noqa: E731
CREDS_TEAMS = {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
CREDS_DAWARICH = {"base_url": "https://x.invalid", "token": "t"}


class FakeOutlook:
    def __init__(self, events, available=True):
        self._events = events
        self._available = available

    def available(self):
        return self._available

    def fetch(self, start, end):
        return self._events


def use_outlook(monkeypatch, fake):
    """Das (einzige) Outlook-Plugin mit einer Fake-Quelle versorgen."""
    monkeypatch.setattr(plugins.get("outlook"), "_calendar", fake)
    monkeypatch.setattr(OutlookCalendar, "available", staticmethod(lambda: fake.available()))


def use_teams(monkeypatch, creds, client_cls=None):
    monkeypatch.setattr("zeitspur.teams.load_credentials", lambda *a, **k: creds)
    monkeypatch.setattr("zeitspur.teams.has_credentials", lambda *a, **k: creds is not None)
    if client_cls is not None:
        monkeypatch.setattr("zeitspur.teams.TeamsCallRecords", client_cls)


def use_dawarich(monkeypatch, creds, client_cls=None):
    monkeypatch.setattr("zeitspur.dawarich.load_credentials", lambda *a, **k: creds)
    monkeypatch.setattr("zeitspur.dawarich.has_credentials", lambda *a, **k: creds is not None)
    if client_cls is not None:
        monkeypatch.setattr("zeitspur.dawarich.DawarichClient", client_cls)


def make_events():
    return [CalendarEvent(ext_id="a", ts_start=T(12), ts_end=T(12, 30), subject="Serverumzug",
                          location="B2", organizer="Anna", attendees="Anna; Tim", category="meeting", extra="{}")]


# --------------------------------------------------------------------------- nur Installiertes laeuft

def test_frische_konfiguration_hat_kein_plugin():
    """Nichts ist standardmaessig aktiv - jedes Plugin muss hinzugefuegt werden."""
    assert Config().installed_plugins == []


def test_nicht_installiertes_plugin_wird_nicht_synchronisiert(storage, monkeypatch):
    """Selbst mit gespeicherten Zugangsdaten: ohne Hinzufuegen kein Abruf."""
    called = []

    class Client:
        def __init__(self, creds):
            called.append(creds)

        def fetch(self, start, end):
            return []

    use_dawarich(monkeypatch, CREDS_DAWARICH, Client)
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    sync = EventSync(Config(installed_plugins=[]), storage)
    report = sync.sync_range(DAY, DAY)
    assert report.total == 0 and not report.errors and not sync.any_enabled()
    assert called == [] and storage.events_between(T(0), T(23, 59)) == []


def test_unbekanntes_plugin_wird_ignoriert(storage, monkeypatch):
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    report = EventSync(Config(installed_plugins=["gibts-nicht", "outlook"]), storage).sync_range(DAY, DAY)
    assert report.counts == {"outlook": 1} and not report.errors


def test_sources_filter_beschraenkt_auf_einzelne_plugins(storage, monkeypatch):
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    use_dawarich(monkeypatch, None)
    sync = EventSync(Config(installed_plugins=["outlook", "dawarich"]), storage)
    report = sync.sync_range(DAY, DAY, sources=["outlook"])
    assert report.counts == {"outlook": 1} and not report.errors   # Dawarich gar nicht angefasst


# --------------------------------------------------------------------------- Outlook

def test_sync_range_writes_outlook(storage, monkeypatch):
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    report = EventSync(Config(installed_plugins=["outlook"]), storage).sync_range(DAY, DAY)
    assert report.counts == {"outlook": 1} and not report.errors
    rows = storage.events_between(T(0), T(23, 59))
    assert [r["subject"] for r in rows] == ["Serverumzug"] and rows[0]["organizer"] == "Anna"
    assert rows[0]["source"] == "outlook"   # Plugin-Id == source in der Datenbank


def test_sync_skips_outlook_when_unavailable(storage, monkeypatch):
    use_outlook(monkeypatch, FakeOutlook(make_events(), available=False))
    report = EventSync(Config(installed_plugins=["outlook"]), storage).sync_range(DAY, DAY)
    assert report.total == 0 and any("Outlook" in e for e in report.errors)
    assert storage.events_between(T(0), T(23, 59)) == []


def test_sync_range_replaces_previous(storage, monkeypatch):
    sync = EventSync(Config(installed_plugins=["outlook"], events_window_days=3), storage)
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    sync.sync_range(DAY, DAY)
    use_outlook(monkeypatch, FakeOutlook([]))  # Termin abgesagt
    sync.sync_range(DAY, DAY)
    assert storage.events_between(T(0), T(23, 59)) == []


def test_outlook_error_does_not_wipe_cached_events(storage, monkeypatch):
    from zeitspur.outlook import OutlookError
    sync = EventSync(Config(installed_plugins=["outlook"]), storage)
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    assert sync.sync_range(DAY, DAY).counts == {"outlook": 1}

    class RaisingOutlook(FakeOutlook):
        def fetch(self, s, e):
            raise OutlookError("COM momentan nicht erreichbar")
    use_outlook(monkeypatch, RaisingOutlook([]))
    report = sync.sync_range(DAY, DAY)
    assert report.total == 0 and any("Outlook" in x for x in report.errors)
    # entscheidend: der vorhandene Termin bleibt erhalten (kein replace_events mit leerer Liste)
    assert len(storage.events_between(T(0), T(23, 59))) == 1


# --------------------------------------------------------------------------- Teams

def test_sync_teams_without_credentials_reports_error(storage, monkeypatch):
    use_teams(monkeypatch, None)
    report = EventSync(Config(installed_plugins=["teams"], teams_user_id="TIM"), storage).sync_range(DAY, DAY)
    assert report.total == 0 and any("Zugangsdaten" in e for e in report.errors)


def test_teams_without_identity_is_skipped(storage, monkeypatch):
    called = []

    class Client:
        def __init__(self, creds, **kw):
            called.append(1)

    use_teams(monkeypatch, CREDS_TEAMS, Client)
    report = EventSync(Config(installed_plugins=["teams"]), storage).sync_range(DAY, DAY)
    # ohne Identitaet werden KEINE firmenweiten Anrufe synchronisiert - nicht einmal abgefragt
    assert report.total == 0 and any("Identit" in e for e in report.errors)
    assert called == [] and storage.events_between(T(0), T(23, 59)) == []


def test_teams_sync_filters_and_persists_cache(storage, monkeypatch):
    from zeitspur.teams import CallEvent

    class FakeClient:
        def __init__(self, creds, **kw):
            pass

        def fetch(self, start, end, *, matcher=None, checked=None):
            assert matcher is not None and matcher.active()
            checked["r-mine"] = True
            checked["r-other"] = False   # Klassifizierung wie im echten fetch
            return [CallEvent(ext_id="r-mine", ts_start=T(12), ts_end=T(12, 5),
                              subject="Teams-Anruf (ausgehend) mit Bob", location="Microsoft Teams",
                              organizer="Tim", attendees="Bob", category="call",
                              extra='{"direction": "outgoing"}')]

    use_teams(monkeypatch, CREDS_TEAMS, FakeClient)
    rep = EventSync(Config(installed_plugins=["teams"], teams_user_id="TIM"), storage).sync_range(DAY, DAY)
    assert rep.counts == {"teams": 1}
    rows = storage.events_between(T(0), T(23, 59))
    assert [r["subject"] for r in rows] == ["Teams-Anruf (ausgehend) mit Bob"]
    # Cache persistiert: beide Klassifizierungen (auch die negative) gemerkt
    assert storage.teams_seen_map() == {"r-mine": True, "r-other": False}


# --------------------------------------------------------------------------- Dawarich

def test_dawarich_without_credentials_reports_error(storage, monkeypatch):
    use_dawarich(monkeypatch, None)
    report = EventSync(Config(installed_plugins=["dawarich"]), storage).sync_range(DAY, DAY)
    assert report.total == 0 and any("Dawarich" in e for e in report.errors)
    assert storage.events_between(T(0), T(23, 59)) == []


def test_dawarich_sync_writes_events(storage, monkeypatch):
    from zeitspur.dawarich import LocationEvent

    class FakeClient:
        def __init__(self, creds):
            pass

        def fetch(self, start, end):
            return [LocationEvent(ext_id="visit-1", ts_start=T(9), ts_end=T(10),
                                  subject="Aufenthalt (52.50000, 13.40000)", location="52.50000, 13.40000",
                                  organizer=None, attendees=None, category="visit", extra="{}")]

    use_dawarich(monkeypatch, CREDS_DAWARICH, FakeClient)
    report = EventSync(Config(installed_plugins=["dawarich"]), storage).sync_range(DAY, DAY)
    assert report.counts == {"dawarich": 1} and not report.errors
    rows = storage.events_between(T(0), T(23, 59))
    assert [r["source"] for r in rows] == ["dawarich"]
    assert rows[0]["location"] == "52.50000, 13.40000"


def test_dawarich_failure_does_not_stop_other_sources(storage, monkeypatch):
    """Der Dawarich-Server ist oefter mal weg - Outlook muss trotzdem synchronisiert werden."""
    from zeitspur.dawarich import DawarichError

    class Broken:
        def __init__(self, creds):
            pass

        def fetch(self, start, end):
            raise DawarichError("Der Dawarich-Server ist gerade nicht erreichbar (502).")

    use_dawarich(monkeypatch, CREDS_DAWARICH, Broken)
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    report = EventSync(Config(installed_plugins=["outlook", "dawarich"]), storage).sync_range(DAY, DAY)
    assert report.counts == {"outlook": 1}
    assert any("Dawarich" in e for e in report.errors)
    assert [r["subject"] for r in storage.events_between(T(0), T(23, 59))] == ["Serverumzug"]


def test_unerwarteter_fehler_eines_plugins_stoppt_die_anderen_nicht(storage, monkeypatch):
    """Auch ein Programmierfehler in einem Plugin darf die uebrigen Quellen nicht mitreissen."""
    class Kaputt:
        def __init__(self, creds):
            pass

        def fetch(self, start, end):
            raise RuntimeError("Fehler im Plugin")

    use_dawarich(monkeypatch, CREDS_DAWARICH, Kaputt)
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    report = EventSync(Config(installed_plugins=["outlook", "dawarich"]), storage).sync_range(DAY, DAY)
    assert report.counts == {"outlook": 1} and any("Fehler im Plugin" in e for e in report.errors)


# --------------------------------------------------------------------------- Bericht

def test_report_summary_und_merge():
    a = SyncReport(counts={"teams": 2})
    a.merge(SyncReport(counts={"teams": 1, "dawarich": 4}, errors=["Dawarich: weg"]))
    assert a.counts == {"teams": 3, "dawarich": 4} and a.total == 7
    assert a.summary() == "3 Microsoft Teams, 4 Standort-Historie (Dawarich), Fehler: Dawarich: weg"
    assert SyncReport().summary() == "keine Plugins installiert"


# --------------------------------------------------------------------------- Thread

def _warte(bedingung, sekunden=30):
    deadline = time.monotonic() + sekunden
    while time.monotonic() < deadline and not bedingung():
        time.sleep(0.05)


def test_thread_runs_and_syncs(storage, monkeypatch):
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    cfg = Config(installed_plugins=["outlook"], events_sync_minutes=1)
    stop = threading.Event()
    job = EventSyncThread(cfg, storage, stop, initial_delay_s=0)
    # sync_window ruft date.today(); wir zwingen das Fenster auf unseren Testtag
    monkeypatch.setattr(job.sync, "sync_window", lambda: job.sync.sync_range(DAY, DAY))
    job.start()
    _warte(lambda: job.runs > 0)
    stop.set()
    job.wake()
    job.join(3)
    assert not job.is_alive() and job.runs >= 1
    assert job.last_report and job.last_report.counts == {"outlook": 1}
    assert len(storage.events_between(T(0), T(23, 59))) == 1


def test_thread_laeuft_ohne_plugin_weiter_und_holt_spaeter_hinzugefuegte_ab(storage, monkeypatch):
    """Frueher beendete sich der Thread ohne aktive Quelle - ein spaeter hinzugefuegtes Plugin
    waere dann bis zum naechsten Programmstart nie synchronisiert worden."""
    use_outlook(monkeypatch, FakeOutlook(make_events()))
    cfg = Config(installed_plugins=[], events_sync_minutes=60)
    stop = threading.Event()
    job = EventSyncThread(cfg, storage, stop, initial_delay_s=0)
    monkeypatch.setattr(job.sync, "sync_window", lambda: job.sync.sync_range(DAY, DAY))
    job.start()
    try:
        time.sleep(0.3)
        assert job.is_alive() and job.runs == 0          # wartet, statt sich zu beenden
        cfg.installed_plugins = ["outlook"]               # Plugin zur Laufzeit hinzugefuegt ...
        job.trigger()                                     # ... und sofortiger Lauf angestossen
        _warte(lambda: job.runs > 0)
        assert job.runs >= 1 and len(storage.events_between(T(0), T(23, 59))) == 1
    finally:
        stop.set()
        job.wake()
        job.join(3)


def test_thread_skips_in_window_requested_days(storage, monkeypatch):
    cfg = Config(installed_plugins=["outlook"], events_window_days=14)
    stop = threading.Event()
    job = EventSyncThread(cfg, storage, stop, initial_delay_s=0)
    calls = {"window": 0, "days": []}

    def fenster():
        calls["window"] += 1
        return SyncReport()

    def tag(d):
        calls["days"].append(d)
        return SyncReport()

    monkeypatch.setattr(job.sync, "sync_window", fenster)
    monkeypatch.setattr(job.sync, "sync_day", tag)
    job.request_day(date.today())                        # im Fenster -> vom Fenster-Sync abgedeckt
    job.request_day(date.today() - timedelta(days=40))   # ausserhalb -> separater Tages-Sync
    job._run_once()
    assert calls["window"] == 1
    assert calls["days"] == [date.today() - timedelta(days=40)]  # nur der Tag ausserhalb des Fensters
