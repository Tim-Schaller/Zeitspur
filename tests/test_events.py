"""Kalender-/Anruf-Ereignisse in der Datenbank (Phase 2)."""
from datetime import datetime

import pytest

from zeitspur import timeutil
from zeitspur.storage import SCHEMA_VERSION, ReadOnlyStorage, Storage

T0 = timeutil.to_ms(datetime(2026, 9, 9, 9, 0, 0))
MIN = 60_000
H = 3_600_000


def ev(ts_start, ts_end, subject, source="outlook", ext_id=None, category="meeting", **extra):
    row = {"ext_id": ext_id, "ts_start": ts_start, "ts_end": ts_end, "subject": subject,
           "category": category, "location": None, "organizer": None, "attendees": None, "extra": None}
    row.update(extra)
    return row


def test_replace_events_and_between(storage):
    events = [ev(T0, T0 + H, "Serverumzug", ext_id="a"), ev(T0 + 2 * H, T0 + 3 * H, "Standup", ext_id="b")]
    assert storage.replace_events("outlook", T0 - H, T0 + 4 * H, events) == 2
    rows = storage.events_between(T0 - MIN, T0 + 5 * H)
    assert [r["subject"] for r in rows] == ["Serverumzug", "Standup"]
    assert rows[0]["source"] == "outlook" and rows[0]["synced_at"] > 0
    # Ueberlappungsfilter
    assert [r["subject"] for r in storage.events_between(T0 + 30 * MIN, T0 + 90 * MIN)] == ["Serverumzug"]
    assert storage.events_between(T0 + 90 * MIN, T0 + 2 * H) == []
    around = storage.events_around(T0 + 30 * MIN, 40 * MIN)
    assert [r["subject"] for r in around] == ["Serverumzug"]


def test_replace_events_is_idempotent_per_window(storage):
    storage.replace_events("outlook", T0, T0 + 4 * H, [ev(T0, T0 + H, "Alt", ext_id="x")])
    # erneuter Sync desselben Fensters ersetzt vollstaendig (geloeschter Termin verschwindet)
    storage.replace_events("outlook", T0, T0 + 4 * H, [ev(T0 + 2 * H, T0 + 3 * H, "Neu", ext_id="y")])
    rows = storage.events_between(T0 - H, T0 + 5 * H)
    assert [r["subject"] for r in rows] == ["Neu"]


def test_replace_events_only_touches_own_source_and_window(storage):
    storage.replace_events("outlook", T0, T0 + 4 * H, [ev(T0, T0 + H, "Outlook-Termin", ext_id="o")])
    storage.replace_events("teams", T0, T0 + 4 * H, [ev(T0, T0 + 20 * MIN, "Anruf", source="teams", category="call", ext_id="t")])
    # Outlook erneut synchronisieren -> Teams bleibt unberuehrt
    storage.replace_events("outlook", T0, T0 + 4 * H, [ev(T0, T0 + H, "Outlook-Termin v2", ext_id="o")])
    rows = storage.events_between(T0 - H, T0 + 5 * H)
    subjects = sorted(r["subject"] for r in rows)
    assert subjects == ["Anruf", "Outlook-Termin v2"]
    # Termin ausserhalb des Sync-Fensters bleibt erhalten
    storage.replace_events("outlook", T0 + 10 * H, T0 + 11 * H, [ev(T0 + 10 * H, T0 + 10 * H + MIN, "Spaeter", ext_id="s")])
    storage.replace_events("outlook", T0, T0 + 4 * H, [])  # leerer Sync des ersten Fensters
    rows = storage.events_between(0, 10 ** 15)
    subjects = sorted(r["subject"] for r in rows)
    assert subjects == ["Anruf", "Spaeter"]


def test_delete_events_older_than(storage):
    storage.replace_events("outlook", 0, 100 * H, [
        ev(T0 - 40 * 24 * H, T0 - 40 * 24 * H + H, "sehr alt", ext_id="1"),
        ev(T0, T0 + H, "frisch", ext_id="2")])
    cutoff = T0 - 14 * 24 * H
    deleted = 0
    while True:
        n = storage.delete_events_older_than(cutoff, batch=1)
        if n == 0:
            break
        deleted += n
    assert deleted == 1
    assert [r["subject"] for r in storage.events_between(0, 10 ** 15)] == ["frisch"]


def test_events_sources_filter(storage):
    storage.replace_events("outlook", T0, T0 + 4 * H, [ev(T0, T0 + H, "Termin", ext_id="o")])
    storage.replace_events("teams", T0, T0 + 4 * H, [ev(T0, T0 + 20 * MIN, "Anruf", source="teams", category="call", ext_id="t")])
    assert [r["subject"] for r in storage.events_between(T0 - H, T0 + 5 * H, sources=["teams"])] == ["Anruf"]
    assert {r["subject"] for r in storage.events_between(T0 - H, T0 + 5 * H, sources=["outlook", "teams"])} == {"Termin", "Anruf"}


def test_schema_is_v2_and_readonly_sees_events(tmp_path, key):
    db = tmp_path / "zeitspur.db"
    w = Storage(db, key)
    assert w.get_meta("schema_version") == str(SCHEMA_VERSION)
    w.replace_events("outlook", T0, T0 + 4 * H, [ev(T0, T0 + H, "Meeting", ext_id="m")])
    r = ReadOnlyStorage(db, key)
    assert [e["subject"] for e in r.events_between(T0 - H, T0 + 5 * H)] == ["Meeting"]
    assert r.event_bounds() == (T0, T0 + H)
    r.close()
    w.close()


def test_migration_v1_to_v2(tmp_path, key):
    """Eine v1-Datenbank (ohne calendar_events) wird beim Oeffnen migriert."""
    import sqlcipher3

    from zeitspur.crypto import open_database
    db = tmp_path / "old.db"
    con = open_database(db, key)
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    con.execute("CREATE TABLE entries(id INTEGER PRIMARY KEY, ts_start INTEGER, ts_end INTEGER, monitor_id INTEGER, "
                "process_name TEXT, window_title TEXT, exe_path TEXT, width INTEGER, height INTEGER, "
                "ocr_status INTEGER, ocr_text TEXT, ocr_conf REAL, created_at INTEGER)")
    con.close()
    s = Storage(db, key)  # sollte migrieren, nicht abstuerzen
    assert s.get_meta("schema_version") == str(SCHEMA_VERSION)
    s.replace_events("outlook", T0, T0 + 4 * H, [ev(T0, T0 + H, "nach Migration", ext_id="z")])
    assert len(s.events_between(T0 - H, T0 + 5 * H)) == 1
    s.close()


def test_teams_seen_cache(storage):
    storage.teams_mark_seen([("call-a", True, T0), ("call-b", False, T0 + H)])
    m = storage.teams_seen_map()
    assert m == {"call-a": True, "call-b": False}
    # Upsert aktualisiert
    storage.teams_mark_seen([("call-b", True, T0 + H)])
    assert storage.teams_seen_map()["call-b"] is True
    # zeitliche Begrenzung
    assert set(storage.teams_seen_map(T0 + 30 * MIN, T0 + 2 * H)) == {"call-b"}
    # Retention
    assert storage.delete_teams_seen_older_than(T0 + 30 * MIN) == 1
    assert set(storage.teams_seen_map()) == {"call-b"}
