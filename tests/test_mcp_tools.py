import asyncio
import io
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import psutil
import pytest
from PIL import Image

from zeitspur import mcp_server, timeutil, winutil
from zeitspur.config import Config
from zeitspur.mcp_server import (ActivityReader, ActivityTools, build_server, claude_desktop_config_paths,
                                 claude_desktop_running, is_claude_desktop_exe, register_claude_desktop,
                                 server_command)
from zeitspur.storage import Storage
from tests.helpers import make_entry, make_webp

DAY = datetime(2026, 9, 9)  # Mittwoch
T = lambda h, m=0, s=0: timeutil.to_ms(DAY.replace(hour=h, minute=m, second=s))  # noqa: E731

# Erfundene Herausgeber-Kennung: Die Suche darf nicht an der echten Kennung der Store-Ausgabe haengen.
STORE_PACKAGE = "Claude_q1w2e3r4t5y6u"
STORE_EXE = r"C:\Program Files\WindowsApps\Claude_2.0.0.0_x64__q1w2e3r4t5y6u\app\Claude.exe"
CLASSIC_EXE = r"C:\Users\Mustermann\AppData\Local\AnthropicClaude\app-1.0.0\claude.exe"
BUNDLED_CODE_EXE = r"C:\Users\Mustermann\AppData\Roaming\Claude\claude-code\2.1.0\abc123\claude.exe"


@pytest.fixture(autouse=True)
def claude_home(tmp_path, monkeypatch):
    """Erfundene AppData-Ordner fuer jeden Test dieser Datei - die echte Claude-Desktop-Konfiguration wird nie
    gelesen oder geschrieben. store/classic: Konfigurationsdatei der Store-Ausgabe bzw. des klassischen Setups."""
    local, roaming = tmp_path / "AppData" / "Local", tmp_path / "AppData" / "Roaming"
    local.mkdir(parents=True)
    roaming.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    package = local / "Packages" / STORE_PACKAGE
    return SimpleNamespace(package=package,
                           store=package / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json",
                           classic=roaming / "Claude" / "claude_desktop_config.json")


@pytest.fixture
def cli(monkeypatch):
    """main() ohne echtes Logging-Setup, ohne echte Meldungsfenster und ohne Blick auf die echten Prozesse.
    Meldungsfenster werden in shown gesammelt; answers sind die Klicks (Standard: OK)."""
    monkeypatch.setattr(mcp_server, "_setup_logging", lambda: None)
    monkeypatch.setattr(mcp_server, "claude_desktop_running", lambda: False)
    boxes = SimpleNamespace(shown=[], answers=[])

    def fake_box(text, title="Zeitspur", flags=0x40):
        boxes.shown.append((text, flags))
        return boxes.answers.pop(0) if boxes.answers else 1   # IDOK

    monkeypatch.setattr(winutil, "message_box", fake_box)
    return boxes


def _write_json(path, data, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding=encoding)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


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
    assert register_claude_desktop(cfg_file) == [cfg_file]
    data = _read_json(cfg_file)
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["Zeitspur"] == server_command()
    assert (tmp_path / "Claude" / "claude_desktop_config.json.bak").exists()
    [fresh] = register_claude_desktop(tmp_path / "neu" / "claude_desktop_config.json")
    assert _read_json(fresh)["mcpServers"]["Zeitspur"]["command"]


def test_config_path_of_store_version(claude_home):
    """Store-/MSIX-Ausgabe: Claude Desktop liest die Datei im Paketordner, nicht unter %APPDATA%\\Claude."""
    _write_json(claude_home.store, {"preferences": {}})
    # andere Pakete mit aehnlichem Namen zaehlen nicht
    _write_json(claude_home.package.parent / "ClaudeHelper_a1b2c3d4e5f6g" / "LocalCache" / "Roaming" / "Claude"
                / "claude_desktop_config.json", {})
    assert claude_desktop_config_paths() == [claude_home.store]


def test_config_paths_when_both_exist(claude_home):
    """Beide Dateien vorhanden (beide Ausgaben installiert oder eine fruehere Registrierung): beide, Store zuerst."""
    _write_json(claude_home.classic, {})
    assert claude_desktop_config_paths() == [claude_home.classic]
    _write_json(claude_home.store, {})
    assert claude_desktop_config_paths() == [claude_home.store, claude_home.classic]


def test_config_path_before_claude_desktop_wrote_one(claude_home):
    """Noch keine Datei: anlegen, wo die installierte Ausgabe liest."""
    assert claude_desktop_config_paths() == [claude_home.classic]   # klassisches Setup (oder noch nichts installiert)
    claude_home.package.mkdir(parents=True)                          # Store-Ausgabe installiert
    assert claude_desktop_config_paths() == [claude_home.store]


def test_register_writes_every_config_claude_desktop_reads(claude_home):
    _write_json(claude_home.store, {"mcpServers": {"Andere": {"command": "x"}}, "preferences": {"a": 1}},
                encoding="utf-8-sig")   # mit BOM, wie von manchem Editor gespeichert
    _write_json(claude_home.classic, {"theme": "dark"})
    assert register_claude_desktop() == [claude_home.store, claude_home.classic]
    store = _read_json(claude_home.store)
    assert store["mcpServers"] == {"Andere": {"command": "x"}, "Zeitspur": server_command()}
    assert store["preferences"] == {"a": 1}
    assert _read_json(claude_home.classic) == {"theme": "dark", "mcpServers": {"Zeitspur": server_command()}}
    # je Datei eine Sicherung des vorherigen Stands
    assert json.loads(claude_home.store.with_suffix(".json.bak").read_text(encoding="utf-8-sig"))["mcpServers"] == {
        "Andere": {"command": "x"}}
    assert _read_json(claude_home.classic.with_suffix(".json.bak")) == {"theme": "dark"}


def test_register_creates_config_of_store_version(claude_home):
    claude_home.package.mkdir(parents=True)
    assert register_claude_desktop() == [claude_home.store]
    assert _read_json(claude_home.store) == {"mcpServers": {"Zeitspur": server_command()}}
    assert not claude_home.classic.exists()


def test_register_writes_one_file_only_once(claude_home):
    """Laeuft der Prozess im Container von Claude Desktop, fuehrt auch %APPDATA%\\Claude zur Datei im Paketordner
    (hier per Hardlink nachgestellt). Sie wird nur einmal geschrieben - sonst ersetzte die zweite Sicherung die
    erste durch den schon geaenderten Stand."""
    _write_json(claude_home.store, {"theme": "dark"})
    claude_home.classic.parent.mkdir(parents=True)
    try:
        os.link(claude_home.store, claude_home.classic)
    except OSError:
        pytest.skip("Dateisystem ohne Hardlinks")
    assert register_claude_desktop() == [claude_home.store]
    assert _read_json(claude_home.store.with_suffix(".json.bak")) == {"theme": "dark"}
    assert _read_json(claude_home.classic)["mcpServers"]["Zeitspur"] == server_command()
    assert not claude_home.classic.with_suffix(".json.bak").exists()


def test_register_leaves_all_files_alone_if_one_is_broken(claude_home):
    _write_json(claude_home.store, {"theme": "dark"})
    claude_home.classic.parent.mkdir(parents=True)
    claude_home.classic.write_text('{"mcpServers": ', encoding="utf-8")
    with pytest.raises(ValueError, match="kein gültiges JSON"):
        register_claude_desktop()
    assert _read_json(claude_home.store) == {"theme": "dark"}
    assert not claude_home.store.with_suffix(".json.bak").exists()
    _write_json(claude_home.classic, {"mcpServers": []})
    with pytest.raises(ValueError, match="mcpServers ist kein Objekt"):
        register_claude_desktop()
    assert _read_json(claude_home.store) == {"theme": "dark"}


@pytest.mark.parametrize("exe, expected", [
    (STORE_EXE, True),
    (STORE_EXE.replace("\\", "/").upper(), True),
    (CLASSIC_EXE, True),
    (r"C:\Users\Mustermann\AppData\Local\AnthropicClaude\claude.exe", True),   # Startprogramm des Setups
    (BUNDLED_CODE_EXE, False),                                                 # Claude Code aus Claude Desktop
    (r"C:\Users\Mustermann\.local\bin\claude.exe", False),                     # Claude Code eigenstaendig
    (r"C:\Program Files\WindowsApps\Claude_2.0.0.0_x64__q1w2e3r4t5y6u\app\resources\helper.exe", False),
    (r"C:\Program Files\WindowsApps\ClaudeHelper_1.0.0.0_x64__q1w2e3r4t5y6u\claude.exe", False),
    (None, False),
])
def test_is_claude_desktop_exe(exe, expected):
    assert is_claude_desktop_exe(exe) is expected


class _FakeProcess:
    def __init__(self, name, exe):
        self.info = {"name": name}
        self._exe = exe

    def exe(self):
        if isinstance(self._exe, Exception):
            raise self._exe
        return self._exe


def test_claude_desktop_running(monkeypatch):
    procs = [_FakeProcess("explorer.exe", r"C:\Windows\explorer.exe"),
             _FakeProcess("claude.exe", BUNDLED_CODE_EXE),               # Claude Code laeuft - zaehlt nicht
             _FakeProcess("claude.exe", psutil.AccessDenied(4711)),      # Pfad nicht lesbar - zaehlt nicht
             _FakeProcess(None, None)]
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter(procs))
    assert claude_desktop_running() is False
    procs.append(_FakeProcess("Claude.exe", STORE_EXE))
    assert claude_desktop_running() is True
    procs[-1] = _FakeProcess("claude.exe", CLASSIC_EXE)
    assert claude_desktop_running() is True


def test_cli_registers_when_claude_desktop_is_closed(claude_home, cli, capsys):
    claude_home.package.mkdir(parents=True)
    assert mcp_server.main(["--register-claude-desktop"]) == 0
    out = capsys.readouterr().out
    assert "eingetragen" in out and str(claude_home.store) in out
    assert _read_json(claude_home.store)["mcpServers"]["Zeitspur"] == server_command()
    assert cli.shown == []


def test_cli_refuses_while_claude_desktop_runs(claude_home, cli, monkeypatch, capsys):
    """Konsole: nichts schreiben, erklaeren, wie man Claude Desktop ganz beendet - und es nie selbst beenden."""
    _write_json(claude_home.store, {"theme": "dark"})
    monkeypatch.setattr(mcp_server, "claude_desktop_running", lambda: True)
    # 4 steht auch in installer/installer.iss (ClaudeDesktopRunningExitCode)
    assert mcp_server.main(["--register-claude-desktop"]) == mcp_server.EXIT_CLAUDE_DESKTOP_RUNNING == 4
    out = capsys.readouterr().out
    assert "vollständig beenden" in out and "„Beenden“" in out and "erneut ausführen" in out
    assert _read_json(claude_home.store) == {"theme": "dark"}
    assert not claude_home.store.with_suffix(".json.bak").exists()


def test_cli_quiet_for_installer(claude_home, cli, monkeypatch):
    """Installer (--quiet, Fenster-Programm ohne Konsole): kein Meldungsfenster, nur der Rueckgabewert."""
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr(mcp_server, "claude_desktop_running", lambda: True)
    assert mcp_server.main(["--register-claude-desktop", "--quiet"]) == 4
    assert not claude_home.classic.exists()
    monkeypatch.setattr(mcp_server, "claude_desktop_running", lambda: False)
    assert mcp_server.main(["--register-claude-desktop", "--quiet"]) == 0
    assert _read_json(claude_home.classic)["mcpServers"]["Zeitspur"] == server_command()
    assert cli.shown == []


def test_cli_window_offers_retry(claude_home, cli, monkeypatch):
    """Fenster-Programm: Hinweis mit "Wiederholen"; ist Claude Desktop danach beendet, wird eingetragen."""
    monkeypatch.setattr("sys.stdout", None)
    running = [True, False]
    monkeypatch.setattr(mcp_server, "claude_desktop_running", lambda: running.pop(0))
    cli.answers = [mcp_server.IDRETRY]
    assert mcp_server.main(["--register-claude-desktop"]) == 0
    (hint, hint_flags), (done, _) = cli.shown
    assert "„Beenden“" in hint and "„Wiederholen“" in hint and hint_flags == mcp_server.MB_RETRYCANCEL_WARNING
    assert str(claude_home.classic) in done
    assert _read_json(claude_home.classic)["mcpServers"]["Zeitspur"] == server_command()


def test_cli_window_cancel_writes_nothing(claude_home, cli, monkeypatch):
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr(mcp_server, "claude_desktop_running", lambda: True)
    cli.answers = [2]   # IDCANCEL
    assert mcp_server.main(["--register-claude-desktop"]) == 4
    assert len(cli.shown) == 1 and not claude_home.classic.exists()


def test_cli_reports_broken_config(claude_home, cli, capsys):
    claude_home.classic.parent.mkdir(parents=True)
    claude_home.classic.write_text("[1, 2]", encoding="utf-8")
    assert mcp_server.main(["--register-claude-desktop"]) == 1
    assert "kein JSON-Objekt" in capsys.readouterr().out
    assert claude_home.classic.read_text(encoding="utf-8") == "[1, 2]"


def test_print_config_names_the_file_claude_desktop_reads(claude_home, cli, capsys):
    _write_json(claude_home.store, {})
    assert mcp_server.main(["--print-config"]) == 0
    out = capsys.readouterr().out
    assert str(claude_home.store) in out and str(claude_home.classic) not in out
    assert '"Zeitspur"' in out and "claude mcp add Zeitspur" in out and "vollständig beenden" in out


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

