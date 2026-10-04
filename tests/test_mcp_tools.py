import asyncio
import io
import json
from datetime import datetime, timedelta

import pytest
from PIL import Image

from zeitspur import timeutil
from zeitspur.config import Config
from zeitspur.mcp_server import (ActivityReader, ActivityTools, build_server, register_claude_desktop, server_command)
from zeitspur.storage import Storage
from tests.helpers import make_entry, make_webp

DAY = datetime(2026, 9, 9)  # Mittwoch
T = lambda h, m=0, s=0: timeutil.to_ms(DAY.replace(hour=h, minute=m, second=s))  # noqa: E731


@pytest.fixture
def seeded(tmp_path, key):
    s = Storage(tmp_path / "zeitspur.db", key)
    ids = {
        "word": make_entry(s, T(11, 50), T(12, 5), process_name="WINWORD.EXE", window_title="Angebot.docx - Word",
                           ocr_text="Angebot für den Serverumzug\nGesamtsumme 12.450 EUR", webp=make_webp(1600, 1000)),
        "teams": make_entry(s, T(12, 5), T(12, 30), process_name="Teams.exe", window_title="Besprechung | Teams",
                            ocr_text="Anna Müller: Termin verschieben?", webp=make_webp(640, 400)),
        "outlook": make_entry(s, T(14, 0), T(14, 10), process_name="OUTLOOK.EXE", window_title="Posteingang - Outlook",
                              ocr_text="Rechnungsnummer 4711 bitte prüfen"),
        "nextday": make_entry(s, T(9) + timeutil.MS_PER_DAY, T(9, 30) + timeutil.MS_PER_DAY, process_name="Code.exe",
                              window_title="storage.py", ocr_text="def insert_entry"),
    }
    s.close()
    cfg = Config(db_path=str(tmp_path / "zeitspur.db"))
    reader = ActivityReader(cfg, key=key)
    yield ActivityTools(reader), ids
    reader.reset()


def test_time_context(seeded):
    tools, ids = seeded
    ctx = tools.get_time_context()
    assert ctx["today"] == datetime.now().date().isoformat()
    assert ctx["weekday"] in ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
    assert len(ctx["last_7_days"]) == 7 and ctx["retention_days"] == 14
    assert ctx["total_entries"] == 4 and ctx["recorded_days"] == ["2026-09-09", "2026-09-10"]
    assert ctx["first_recorded"].startswith("2026-09-09T11:50:00")


def test_search_activity(seeded):
    tools, ids = seeded
    res = tools.search_activity("rechnung")
    assert res["count"] == 1 and res["results"][0]["entry_id"] == ids["outlook"]
    hit = res["results"][0]
    assert hit["app"] == "OUTLOOK.EXE" and "[Rechnungsnummer]" in hit["snippet"] and hit["start"].startswith("2026-09-09T14:00")
    assert tools.search_activity("muller")["results"][0]["entry_id"] == ids["teams"]
    assert tools.search_activity("serverumzug", from_date="2026-09-09", to_date="2026-09-09")["count"] == 1
    assert tools.search_activity("serverumzug", from_date="2026-09-10")["count"] == 0
    assert tools.search_activity("insert", from_date="2026-09-10 08:00", to_date="2026-09-10 10:00")["count"] == 1
    assert tools.search_activity("kaffee")["count"] == 0
    with pytest.raises(ValueError):
        tools.search_activity("   ")


def test_get_activity_at(seeded):
    tools, ids = seeded
    res = tools.get_activity_at("2026-09-09 12:00", window_minutes=15)
    assert res["count"] == 2 and [e["entry_id"] for e in res["entries"]] == [ids["word"], ids["teams"]]
    assert res["entries"][0]["text"].startswith("Angebot") and res["entries"][0]["window_title"] == "Angebot.docx - Word"
    assert [b["app"] for b in res["blocks"]] == ["WINWORD.EXE", "Teams.exe"]
    assert res["blocks"][0]["duration"] == "15 min"
    assert res["from"].startswith("2026-09-09T11:45") and res["to"].startswith("2026-09-09T12:15")
    short = tools.get_activity_at("09.09.2026 12:00", window_minutes=15, max_text_chars=50)
    assert short["entries"][0]["text"].endswith("…") or len(short["entries"][0]["text"]) <= 52
    assert tools.get_activity_at("2026-09-09 03:00")["count"] == 0
    no_text = tools.get_activity_at("2026-09-09T12:00:00", include_text=False)
    assert "text" not in no_text["entries"][0]
    with pytest.raises(ValueError):
        tools.get_activity_at("letzten Mittwoch")


def test_get_activity_at_truncates_many_entries(tmp_path, key):
    s = Storage(tmp_path / "many.db", key)
    for i in range(100):
        make_entry(s, T(12) + i * 5000, T(12) + i * 5000 + 4000, process_name="app.exe", window_title="w")
    s.close()
    tools = ActivityTools(ActivityReader(Config(db_path=str(tmp_path / "many.db")), key=key))
    res = tools.get_activity_at("2026-09-09 12:04", window_minutes=10)
    assert res["entries_truncated"] and len(res["entries"]) == 60 and res["count"] == 60
    assert res["blocks"][0]["entries"] == 100 and len(res["blocks"][0]["entry_ids"]) == 20
    tools.reader.reset()


def test_list_active_apps(seeded):
    tools, ids = seeded
    res = tools.list_active_apps("2026-09-09")
    assert res["date"] == "2026-09-09" and res["weekday"] == "Mittwoch" and res["total_entries"] == 3
    assert [a["app"] for a in res["apps"]] == ["Teams.exe", "WINWORD.EXE", "OUTLOOK.EXE"]  # nach Dauer
    assert res["apps"][0]["duration"] == "25 min" and res["apps"][0]["top_windows"][0]["window_title"] == "Besprechung | Teams"
    assert res["active_duration"] == "50 min"
    assert tools.list_active_apps("2026-09-01")["apps"] == []


def test_get_entry_and_screenshot(seeded):
    tools, ids = seeded
    entry = tools.get_entry(ids["word"])
    assert entry["text"].startswith("Angebot") and entry["next_entry_id"] == ids["teams"] and entry["previous_entry_id"] is None
    data, fmt = tools.get_screenshot_bytes(ids["word"], max_width=800)
    img = Image.open(io.BytesIO(data))
    assert fmt == "jpeg" and img.format == "JPEG" and img.size == (800, 500)
    data2, _ = tools.get_screenshot_bytes(ids["teams"], max_width=1280)
    assert Image.open(io.BytesIO(data2)).size == (640, 400)  # kleiner als max_width: nicht hochskaliert
    with pytest.raises(ValueError):
        tools.get_entry(999)
    with pytest.raises(ValueError):
        tools.get_screenshot_bytes(999)


def test_reader_errors_without_setup(tmp_path, monkeypatch):
    tools = ActivityTools(ActivityReader(Config(db_path=str(tmp_path / "missing.db"))))
    ctx = tools.get_time_context()
    assert "database" in ctx and "existiert noch nicht" in ctx["database"]
    with pytest.raises(RuntimeError):
        tools.search_activity("x")


def test_mcp_server_registers_tools(seeded):
    tools, ids = seeded
    server = build_server(tools.reader)
    listed = asyncio.run(server.list_tools())
    names = {t.name for t in listed}
    assert names == {"get_time_context", "search_activity", "get_activity_at", "list_active_apps", "get_entry", "get_screenshot", "get_calendar"}
    assert all(t.description for t in listed)


def test_register_claude_desktop_merges_config(tmp_path):
    cfg_file = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg_file.parent.mkdir()
    cfg_file.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}), encoding="utf-8")
    path = register_claude_desktop(cfg_file)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["Zeitspur"] == server_command()
    assert (tmp_path / "Claude" / "claude_desktop_config.json.bak").exists()
    fresh = register_claude_desktop(tmp_path / "neu" / "claude_desktop_config.json")
    assert json.loads(fresh.read_text(encoding="utf-8"))["mcpServers"]["Zeitspur"]["command"]


def test_server_command_variants(monkeypatch, tmp_path):
    # Entwicklungsmodus: Python-Interpreter + Modul
    cmd = server_command()
    assert cmd["command"].lower().endswith("python.exe") and cmd["args"] == ["-m", "zeitspur.mcp_server"]
    # gepackt als Zeitspur.exe -> "--mcp"-Modus
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(tmp_path / "Zeitspur.exe"))
    cmd = server_command()
    assert cmd["command"].endswith("Zeitspur.exe") and cmd["args"] == ["--mcp"]
    # gepackt als separate ZeitspurMCP.exe -> direkt
    monkeypatch.setattr("sys.executable", str(tmp_path / "ZeitspurMCP.exe"))
    cmd = server_command()
    assert cmd["command"].endswith("ZeitspurMCP.exe") and "args" not in cmd


def _seed_events(tmp_path, key):
    from zeitspur.storage import Storage
    s = Storage(tmp_path / "zeitspur.db", key)
    s.replace_events("outlook", T(11), T(13), [{"ext_id": "m1", "ts_start": T(11, 50), "ts_end": T(12, 30),
        "subject": "Serverumzug", "category": "meeting", "location": "B2", "organizer": "Anna Müller",
        "attendees": "Anna Müller; Tim", "extra": None}])
    s.replace_events("teams", T(11), T(13), [{"ext_id": "c1", "ts_start": T(12, 5), "ts_end": T(12, 20),
        "subject": "Teams-Anruf mit Bob", "category": "call", "location": "Microsoft Teams", "organizer": "Bob",
        "attendees": "Bob", "extra": None}])
    s.close()


def test_calendar_in_activity_and_apps(seeded, tmp_path, key):
    tools, ids = seeded
    _seed_events(tmp_path, key)
    tools.reader.reset()
    res = tools.get_activity_at("2026-09-09 12:00", window_minutes=15)
    assert "calendar" in res and {c["source"] for c in res["calendar"]} == {"outlook", "teams"}
    meeting = [c for c in res["calendar"] if c["source"] == "outlook"][0]
    assert meeting["subject"] == "Serverumzug" and meeting["category"] == "Besprechung" and meeting["organizer"] == "Anna Müller"
    apps = tools.list_active_apps("2026-09-09")
    assert len(apps["calendar"]) == 2


def test_get_calendar_tool(seeded, tmp_path, key):
    tools, ids = seeded
    _seed_events(tmp_path, key)
    tools.reader.reset()
    cal = tools.get_calendar("2026-09-09")
    assert cal["count"] == 2 and cal["weekday"] == "Mittwoch"
    assert [e["source"] for e in cal["events"]] == ["outlook", "teams"]  # nach ts_start sortiert
    assert tools.get_calendar("2026-09-01")["count"] == 0


def _seed_location_day(tmp_path, key, lat, lon, known=()):
    """Ein Tag mit Aufenthalt, Outlook-Termin und Bildschirmarbeit - wie fuer 'Was habe ich um 9 gemacht?'."""
    s = Storage(tmp_path / "zeitspur.db", key)
    make_entry(s, T(9), T(9, 30), process_name="devenv.exe", window_title="Beispielprojekt - Visual Studio",
               ocr_text="Beispielprojekt Projektmappe")
    s.replace_events("dawarich", T(0), T(23, 59), [{
        "ext_id": "visit-1", "ts_start": T(8, 20), "ts_end": T(12, 0),
        "subject": f"Aufenthalt ({lat}, {lon})", "location": f"{lat}, {lon}",
        "organizer": None, "attendees": None, "category": "visit",
        "extra": json.dumps({"latitude": lat, "longitude": lon})}])
    s.replace_events("outlook", T(0), T(23, 59), [{
        "ext_id": "m1", "ts_start": T(8, 45), "ts_end": T(9, 45), "subject": "Kundentermin Beispiel AG",
        "location": "Hamburg", "organizer": "Mustermann, Max", "attendees": "Max; Beispiel",
        "category": "meeting", "extra": None}])
    s.close()
    cfg = Config(db_path=str(tmp_path / "zeitspur.db"), known_places=list(known))
    return ActivityTools(ActivityReader(cfg, key=key)), cfg


def test_get_activity_at_names_a_known_place(tmp_path, key):
    """'Wo war ich um 9?' soll 'Buero' ergeben, nicht Koordinaten."""
    tools, _ = _seed_location_day(tmp_path, key, 52.51627, 13.37770, known=["Buero;52.5163;13.3777;200"])
    res = tools.get_activity_at("2026-09-09 09:00", window_minutes=30, include_text=False)
    loc = res["location"]
    assert loc["place"] == "Buero" and loc["kind"] == "Aufenthalt" and loc["covers_timestamp"] is True
    assert loc["coordinates"]  # Koordinaten bleiben zusaetzlich erhalten
    # der Aufenthalt traegt im Kalender ebenfalls den Namen
    visit = next(e for e in res["calendar"] if e["source"] == "dawarich")
    assert visit["subject"] == "Buero" and visit["place"] == "Buero"
    # und der Outlook-Termin steht daneben, damit beides in einer Antwort kombiniert werden kann
    assert any("Beispiel AG" in e["subject"] for e in res["calendar"])
    assert any("Visual Studio" in (b.get("window_title") or "") for b in res["blocks"])


def test_get_activity_at_keeps_unknown_place_honest(tmp_path, key):
    """Unbekannter Ort: kein geratener Name, nur Koordinaten."""
    tools, _ = _seed_location_day(tmp_path, key, 48.1371, 11.5754)  # Muenchen, nicht konfiguriert
    loc = tools.get_activity_at("2026-09-09 09:00", window_minutes=30, include_text=False)["location"]
    assert loc["place"] is None
    assert loc["coordinates"] == "48.1371, 11.5754"


def test_get_activity_at_without_location_data(seeded):
    """Ohne Standortquelle darf kein Ort behauptet werden."""
    tools, _ = seeded
    assert tools.get_activity_at("2026-09-09 12:00", window_minutes=30, include_text=False)["location"] is None


def test_release_ausgabe_ohne_standort(tmp_path, key, monkeypatch):
    """Release-Paket: kein 'location'-Feld, keine Orte, und Claude erfaehrt in den Anweisungen nichts davon."""
    import sys

    from zeitspur import edition, mcp_server
    tools, _ = _seed_location_day(tmp_path, key, 48.1371, 11.5754)
    monkeypatch.setattr(edition, "LOCATIONS", False)
    monkeypatch.setitem(sys.modules, "zeitspur.dawarich", None)   # Modul fehlt wie im Release-Paket
    res = tools.get_activity_at("2026-09-09 09:00", window_minutes=30, include_text=False)
    assert "location" not in res and tools.places() == []
    text = mcp_server.instructions()
    assert not any(w in text for w in ("Standort", "Aufenthalt", "Koordinaten", "Buero"))
    assert "get_calendar" in text      # der Rest der Anweisungen ist vollstaendig da

