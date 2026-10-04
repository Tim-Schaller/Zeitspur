import sys
import base64
import io
import threading
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from zeitspur import timeutil
from zeitspur.capture import CaptureState
from zeitspur.config import Config, ConfigError
from zeitspur.timeline_ui.bridge import (Bridge, assign_series, build_html, coerce_config,
                                            format_event, group_blocks)
from tests.helpers import make_entry, make_webp

T0 = timeutil.to_ms(datetime(2026, 9, 9, 10, 0, 0))
MIN = 60_000


class FakeApp:
    """Minimale App-Attrappe mit dem, was die Bridge braucht."""

    def __init__(self, storage, key):
        self.storage = storage
        self.key = key
        self.cfg = Config()
        self.ready = threading.Event()
        self.ready.set()
        self.first_run = False
        self.data_dir = Path(storage.db_path).parent
        self.engine = None
        self.window = None
        self.paused = False
        self.deleted = []
        self.applied = None

    def capture_state(self):
        return CaptureState.RECORDING, "Aufnahme läuft", ""

    def is_paused(self):
        return self.paused

    def set_paused(self, paused):
        self.paused = paused

    def ocr_available(self):
        return False

    def status_summary(self):
        return {"ready": True, "entries_today": 0}

    def update_status(self):
        return None   # wie im eigenen Build: kein Update-Kanal

    def delete_entry(self, entry_id):
        self.deleted.append(entry_id)
        return self.storage.delete_entry(entry_id)

    def delete_recent(self, minutes):
        return 0

    def apply_config(self, cfg):
        self.applied = cfg
        return cfg.db_path != self.cfg.db_path

    def complete_first_run(self, cfg):
        self.first_run = False
        return True

    def request_event_sync(self, day):
        self.synced_day = day

    # Plugins: Zugangsdaten gehen durch die echten Plugins (DPAPI im isolierten Testordner),
    # damit Speichern und Wiederauslesen wirklich zusammenpassen.
    def plugin_list(self):
        from zeitspur import plugins
        return [p.describe(self.cfg) for p in plugins.PLUGINS]

    def add_plugin(self, plugin_id):
        self.cfg.installed_plugins = [*self.cfg.installed_plugins, plugin_id]
        return {"id": plugin_id, "installed": True}

    def remove_plugin(self, plugin_id):
        self.cfg.installed_plugins = [p for p in self.cfg.installed_plugins if p != plugin_id]
        return {"removed_events": 0}

    def save_plugin_credentials(self, plugin_id, values):
        self.saved_creds = (plugin_id, values)
        return "Verbindung erfolgreich."

    def reveal_plugin_credentials(self, plugin_id):
        from zeitspur import plugins
        return plugins.get(plugin_id).reveal_credentials()

    def clear_plugin_credentials(self, plugin_id):
        return True

    def test_plugin(self, plugin_id):
        return "Verbindung erfolgreich."


@pytest.fixture
def bridge(storage, key):
    b = Bridge(FakeApp(storage, key))
    yield b
    b._close()


def test_group_blocks_merges_consecutive_same_window():
    entries = [
        {"id": 1, "ts_start": T0, "ts_end": T0 + 5000, "process_name": "code.exe", "window_title": "a.py", "ocr_status": 1},
        {"id": 2, "ts_start": T0 + 6000, "ts_end": T0 + 20000, "process_name": "code.exe", "window_title": "a.py", "ocr_status": 0},
        {"id": 3, "ts_start": T0 + 21000, "ts_end": T0 + 30000, "process_name": "code.exe", "window_title": "b.py", "ocr_status": 1},
        {"id": 4, "ts_start": T0 + 31000, "ts_end": T0 + 40000, "process_name": "code.exe", "window_title": "a.py", "ocr_status": 1},
        {"id": 5, "ts_start": T0 + 120000, "ts_end": T0 + 125000, "process_name": "code.exe", "window_title": "a.py", "ocr_status": 1},
    ]
    blocks = group_blocks(entries, gap_ms=15000)
    assert [b["ids"] for b in blocks] == [[1, 2], [3], [4], [5]]  # Titelwechsel und grosse Luecke trennen
    assert blocks[0]["ts_end"] == T0 + 20000 and blocks[0]["count"] == 2 and blocks[0]["ocr_pending"] == 1
    # ohne Zuordnung bekommt alles den neutralen Ton; mit Zuordnung die Farbe der App
    assert blocks[0]["color"] == blocks[1]["color"]
    farben = assign_series({"code.exe": 100})
    mit = group_blocks(entries, gap_ms=15000, series=farben)
    assert mit[0]["color"] == farben["code.exe"]["color"]
    assert mit[0]["text_color"] == farben["code.exe"]["text"]
    assert group_blocks([], 1000) == []


def test_build_html_inlines_assets():
    html = build_html()
    assert "/*__CSS__*/" not in html and "/*__JS__*/" not in html
    assert "pywebviewready" in html and "<style>" in html and "http://" not in html.split("<body")[0]


def test_coerce_config_from_form_values():
    cfg = coerce_config({
        "capture_interval_seconds": "10", "retention_days": 7.0, "change_threshold": "0.02",
        "excluded_process_regex": "KeePass.*\n\n  .*Banking.*  \n", "excluded_title_regex": ["a", " b "],
        "ocr_psm": "11", "log_level": "debug", "db_path": " D:/zeitspur/zeitspur.db ",
    })
    assert cfg.capture_interval_seconds == 10 and cfg.retention_days == 7 and cfg.change_threshold == 0.02
    assert cfg.excluded_process_regex == ["KeePass.*", ".*Banking.*"] and cfg.excluded_title_regex == ["a", "b"]
    assert cfg.ocr_psm == 11 and cfg.log_level == "DEBUG" and cfg.db_path == "D:/zeitspur/zeitspur.db"
    assert cfg.webp_quality == 75  # unveraendert
    with pytest.raises(ConfigError):
        coerce_config({"capture_interval_seconds": "abc"})
    with pytest.raises(ConfigError):
        coerce_config({"webp_quality": 99})
    base = Config(webp_quality=60)
    assert coerce_config({"retention_days": 3}, base).webp_quality == 60


def test_get_state_and_days(bridge, storage):
    info = bridge.get_state()
    assert info["ready"] and not info["first_run"] and info["capture_state"] == "recording"
    assert info["today"] == timeutil.local_date(timeutil.now_ms()).isoformat()
    assert bridge.list_days() == []
    make_entry(storage, T0)
    assert bridge.list_days() == ["2026-09-09"]


def test_get_day_groups_lanes_and_blocks(bridge, storage):
    a = make_entry(storage, T0, T0 + 5000, process_name="code.exe", window_title="a.py")
    b = make_entry(storage, T0 + 5000, T0 + 3 * MIN, process_name="code.exe", window_title="a.py")
    c = make_entry(storage, T0 + 3 * MIN, T0 + 5 * MIN, process_name="outlook.exe", window_title="Posteingang")
    m2 = make_entry(storage, T0, T0 + 4 * MIN, monitor_id=2, process_name="code.exe", window_title="a.py")
    day = bridge.get_day("2026-09-09")
    assert day["total_entries"] == 4 and day["date"] == "2026-09-09"
    assert [lane["monitor_id"] for lane in day["lanes"]] == [1, 2]
    lane1 = day["lanes"][0]["blocks"]
    assert [blk["ids"] for blk in lane1] == [[a, b], [c]]
    assert lane1[0]["ts_start"] == T0 and lane1[0]["ts_end"] == T0 + 3 * MIN
    assert day["lanes"][1]["blocks"][0]["ids"] == [m2]
    assert day["active_ms"] == 5 * MIN  # laengste Spur
    assert day["first_ms"] == T0 and day["last_ms"] == T0 + 5 * MIN
    empty = bridge.get_day("2026-09-01")
    assert empty["total_entries"] == 0 and empty["lanes"] == [] and empty["first_ms"] is None


def test_get_entry_and_thumbnail(bridge, storage):
    small = make_webp(320, 200)
    big = make_webp(1600, 1000, color=(200, 50, 50))
    e1 = make_entry(storage, T0, ocr_text="Text eins", webp=small)
    e2 = make_entry(storage, T0 + MIN, webp=big)
    entry = bridge.get_entry(e1)
    assert entry["ocr_text"] == "Text eins" and entry["date"] == "2026-09-09"
    assert entry["start_label"] == "10:00:00" and entry["prev_id"] is None and entry["next_id"] == e2
    assert entry["ocr_status_label"] == "Text erkannt"
    assert bridge.get_entry(999) is None
    t1 = bridge.get_thumbnail(e1, 800)
    assert t1["data_uri"].startswith("data:image/webp;base64,") and (t1["width"], t1["height"]) == (320, 200)
    assert base64.b64decode(t1["data_uri"].split(",", 1)[1]) == small  # klein genug: unveraendert durchgereicht
    t2 = bridge.get_thumbnail(e2, 640)
    assert t2["data_uri"].startswith("data:image/jpeg;base64,") and t2["width"] == 640 and t2["height"] == 400
    img = Image.open(io.BytesIO(base64.b64decode(t2["data_uri"].split(",", 1)[1])))
    assert img.size == (640, 400)
    assert bridge.get_thumbnail(e2, 640) is t2  # Cache
    bridge._invalidate_cache(e2)
    assert bridge.get_thumbnail(e2, 640) is not t2
    assert bridge.get_thumbnail(999) is None


def test_search_results_have_labels(bridge, storage):
    eid = make_entry(storage, T0, ocr_text="Rechnungsnummer 4711\nZeile zwei", window_title="Word")
    rows = bridge.search("rechnung")
    assert len(rows) == 1 and rows[0]["id"] == eid
    assert rows[0]["date"] == "2026-09-09" and rows[0]["time_label"] == "10:00"
    assert "Rechnungsnummer" in rows[0]["snippet"] and "\n" not in rows[0]["snippet"]
    assert bridge.search("rechnung", from_date="2026-09-10") == []
    assert bridge.search("rechnung", to_date="2026-09-10")[0]["id"] == eid
    assert bridge.search("") == []


def test_actions_delegate_to_app(bridge, storage):
    eid = make_entry(storage, T0)
    assert bridge.delete_entry(eid) == {"deleted": 1} and bridge._app.deleted == [eid]
    info = bridge.set_paused(True)
    assert info["paused"] is True
    res = bridge.save_config({"retention_days": 30})
    assert res == {"ok": True, "restart_required": False} and bridge._app.applied.retention_days == 30
    with pytest.raises(ValueError):
        bridge.save_config({"retention_days": 0})
    assert bridge.get_config()["capture_interval_seconds"] == 5


def test_bridge_requires_ready_database(storage, key):
    app = FakeApp(storage, key)
    app.ready.clear()
    b = Bridge(app)
    with pytest.raises(RuntimeError):
        b.list_days()
    assert b.get_state()["ready"] is False


def test_assign_series_prefers_icon_colours(monkeypatch):
    """Die Farbe soll vom Programmsymbol kommen - die kennt der Nutzer bereits."""
    from zeitspur import appicon
    from zeitspur.timeline_ui import bridge as bridge_mod

    icons = {"a.exe": (13, 130, 209), "b.exe": (217, 111, 76)}   # blau, orange
    monkeypatch.setattr(bridge_mod.appicon if hasattr(bridge_mod, "appicon") else appicon,
                        "icon_color", lambda path: icons.get(path))
    monkeypatch.setattr(appicon, "icon_color", lambda path: icons.get(path))
    res = assign_series({"A": 10, "B": 5}, {"A": "a.exe", "B": "b.exe"})
    a = appicon.to_hex(appicon.normalize(icons["a.exe"]))
    assert res["A"]["color"] == a            # meistgenutzte App behaelt ihre Symbolfarbe unveraendert
    assert res["B"]["color"].startswith("#") and res["B"]["color"] != res["A"]["color"]


def test_assign_series_separates_similar_icons(monkeypatch):
    """Vier blaue Symbole duerfen nicht in vier gleichen Blaus enden."""
    import itertools

    from zeitspur import appicon

    blues = {f"p{i}.exe": (13 + i * 6, 100 + i * 4, 209 - i * 5) for i in range(4)}
    monkeypatch.setattr(appicon, "icon_color", lambda path: blues.get(path))
    res = assign_series({f"A{i}": 10 - i for i in range(4)},
                        {f"A{i}": f"p{i}.exe" for i in range(4)})
    def rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))
    for x, y in itertools.combinations(res, 2):
        d, _ = appicon.separation(rgb(res[x]["color"]), rgb(res[y]["color"]))
        assert d >= 9, (x, y, d, res[x]["color"], res[y]["color"])


def test_assign_series_without_icons_uses_palette_and_neutral(monkeypatch):
    from zeitspur import appicon
    from zeitspur.timeline_ui.bridge import OTHER_COLOR, SERIES_COLORS

    monkeypatch.setattr(appicon, "icon_color", lambda path: None)   # nur graue Symbole
    weights = {f"app{i}": 100 - i for i in range(9)}
    res = assign_series(weights, {})
    farben = [res[n]["color"] for n in weights]
    assert farben[0] == SERIES_COLORS[0]                 # erster Platz unveraendert
    assert farben[-1] == farben[-2] == OTHER_COLOR       # ueber die Palette hinaus neutral
    assert len(set(farben[:len(SERIES_COLORS)])) == len(SERIES_COLORS)
    assert assign_series({}, {}) == {}
    assert assign_series(weights, {}) == res             # stabil bei gleicher Eingabe


def _ev(storage, ts_start, ts_end, subject, source='outlook', category='meeting'):
    storage.replace_events(source, ts_start, ts_end, [{'ext_id': subject, 'ts_start': ts_start, 'ts_end': ts_end,
        'subject': subject, 'category': category, 'location': 'B2', 'organizer': 'Anna', 'attendees': 'Anna; Tim', 'extra': None}])


def test_get_day_includes_events(bridge, storage):
    make_entry(storage, T0, T0 + 5 * MIN, process_name="code.exe", window_title="a.py")
    _ev(storage, T0, T0 + 30 * MIN, "Serverumzug")
    day = bridge.get_day("2026-09-09")
    assert "events" in day and len(day["events"]) == 1
    e = day["events"][0]
    assert e["subject"] == "Serverumzug" and e["source"] == "outlook" and e["source_label"] == "Outlook"
    assert e["category_label"] == "Besprechung" and e["organizer"] == "Anna" and e["color"].startswith("#")
    assert e["start_label"] == "10:00" and "min" in e["duration_label"]
    assert bridge._app.synced_day is not None  # On-Demand-Sync angestossen


def test_format_event():
    e = format_event({"id": 1, "source": "teams", "category": "call", "ts_start": T0, "ts_end": T0 + 10 * MIN,
                      "subject": "", "location": "Microsoft Teams", "organizer": None, "attendees": "Anna"})
    assert e["source_label"] == "Teams" and e["category_label"] == "Anruf"
    assert e["subject"] == "(ohne Betreff)" and e["attendees"] == "Anna"


def test_coerce_config_booleans_from_select_strings():
    cfg = coerce_config({"map_enabled": "true"}, Config())
    assert cfg.map_enabled is True
    cfg2 = coerce_config({"map_enabled": "false"}, Config(map_enabled=True))
    assert cfg2.map_enabled is False
    cfg3 = coerce_config({"map_enabled": "Ein"}, Config(map_enabled=False))
    assert cfg3.map_enabled is False  # nur explizite Wahrheitswerte zaehlen


def test_settings_formular_ueberschreibt_keine_plugins():
    """Das Hauptformular kennt installed_plugins nicht - Speichern darf die Plugins nicht abwaehlen."""
    cfg = coerce_config({"retention_days": "10"}, Config(installed_plugins=["teams", "dawarich"]))
    assert cfg.installed_plugins == ["teams", "dawarich"] and cfg.retention_days == 10


def test_plugin_bridge_methods_delegate(bridge):
    res = bridge.save_plugin_credentials("teams", {"tenant_id": " t ", "client_id": "c", "client_secret": " s "})
    # Kennungen werden getrimmt, Geheimnisse nicht (Leerzeichen koennten dazugehoeren)
    assert res["ok"] and bridge._app.saved_creds == ("teams", {"tenant_id": "t", "client_id": "c",
                                                               "client_secret": " s "})
    with pytest.raises(ValueError, match="Tenant-Id"):
        bridge.save_plugin_credentials("teams", {"tenant_id": "", "client_id": "c", "client_secret": "s"})
    with pytest.raises(KeyError):
        bridge.save_plugin_credentials("gibts-nicht", {})
    assert bridge.test_plugin("teams")["message"] == "Verbindung erfolgreich."
    assert bridge.clear_plugin_credentials("teams")["cleared"] is True

    assert bridge.get_state()["installed_plugins"] == []
    bridge.add_plugin("dawarich")
    assert bridge.get_state()["installed_plugins"] == ["dawarich"]
    listed = {p["id"]: p for p in bridge.list_plugins()}
    assert set(listed) == {"outlook", "teams", "teams_local", "dawarich"}
    assert listed["dawarich"]["installed"] and not listed["teams"]["installed"]
    assert [f["key"] for f in listed["teams"]["setting_fields"]] == ["teams_user_id", "teams_user_names"]
    assert [f["kind"] for f in listed["dawarich"]["credential_fields"]] == ["text", "secret"]
    bridge.remove_plugin("dawarich")
    assert bridge.get_state()["installed_plugins"] == []


def test_autostart_goes_to_registry_not_config(bridge, monkeypatch):
    """Der Autostart-Schalter gehoert in die Registry - und das Formular liefert Strings:
    'false' darf auf keinen Fall als True ankommen (bool('false') waere True)."""
    from zeitspur.timeline_ui import bridge as bridge_mod

    state = {"on": False}
    calls = []

    def fake_set(enabled):
        calls.append(enabled)
        state["on"] = enabled
        return enabled

    monkeypatch.setattr(bridge_mod.autostart, "is_enabled", lambda: state["on"])
    monkeypatch.setattr(bridge_mod.autostart, "set_enabled", fake_set)

    assert bridge.get_config()["autostart"] is False
    res = bridge.save_config({"autostart": "false", "retention_days": "14"})
    assert calls == [False] and res["autostart"] is False
    res = bridge.save_config({"autostart": "true", "retention_days": "14"})
    assert calls == [False, True] and res["autostart"] is True
    assert bridge.get_config()["autostart"] is True
    # der Schalter darf nicht in der Config landen (Config kennt kein Feld 'autostart')
    assert not hasattr(bridge._app.applied, "autostart")


def test_save_config_without_autostart_leaves_registry_alone(bridge, monkeypatch):
    from zeitspur.timeline_ui import bridge as bridge_mod

    touched = []
    monkeypatch.setattr(bridge_mod.autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(bridge_mod.autostart, "set_enabled", lambda e: touched.append(e))
    res = bridge.save_config({"retention_days": "21"})
    assert touched == [] and "autostart" not in res


def test_format_event_exposes_geometry_for_the_map():
    from zeitspur.timeline_ui.bridge import format_event
    visit = format_event({"id": 1, "source": "dawarich", "category": "visit", "ts_start": 0, "ts_end": 60000,
                          "subject": "Aufenthalt", "location": "52.5, 13.4", "organizer": None, "attendees": None,
                          "extra": '{"latitude": 52.5, "longitude": 13.4}'})
    assert visit["geo"] == {"lat": 52.5, "lon": 13.4}
    track = format_event({"id": 2, "source": "dawarich", "category": "track", "ts_start": 0, "ts_end": 60000,
                          "subject": "Fahrt", "location": None, "organizer": None, "attendees": None,
                          "extra": '{"path": [[52.5, 13.4], [52.6, 13.5]]}'})
    assert track["geo"] == {"path": [[52.5, 13.4], [52.6, 13.5]]}
    # Termine ohne Koordinaten bekommen nichts untergeschoben
    meeting = format_event({"id": 3, "source": "outlook", "category": "meeting", "ts_start": 0, "ts_end": 60000,
                            "subject": "Standup", "location": "B2", "organizer": None, "attendees": None, "extra": None})
    assert meeting["geo"] is None
    # kaputtes extra darf nicht durchschlagen
    broken = format_event({"id": 4, "source": "dawarich", "category": "visit", "ts_start": 0, "ts_end": 1,
                           "subject": "x", "location": None, "organizer": None, "attendees": None, "extra": "{kaputt"})
    assert broken["geo"] is None


def test_build_html_bundles_leaflet_without_external_references():
    from zeitspur.timeline_ui.bridge import build_html
    html = build_html()
    assert "leafletjs.com" in html            # Leaflet ist mitgeliefert, nicht per CDN
    for placeholder in ("__LEAFLET_CSS__", "__LEAFLET_JS__", "__CSS__", "__JS__"):
        assert placeholder not in html
    # keine nachgeladenen Skripte/Stylesheets - Kacheln holt erst die eingeschaltete Karte
    assert "<script src=" not in html and "<link rel=\"stylesheet\"" not in html


def test_get_state_reports_map_home(bridge):
    """Die Karte braucht einen Startpunkt, sonst haette sie ohne Aufenthalte keinen Bezug."""
    info = bridge.get_state()
    home = info["map_home"]
    assert set(home) == {"lat", "lon", "zoom", "label"}
    assert 47.0 < home["lat"] < 55.0 and 5.0 < home["lon"] < 15.0    # ab Werk: Blick auf Deutschland
    assert home["label"] == ""                    # kein eingebauter Ort - den benennt jeder selbst
    assert info["map_enabled"] is False          # Karte bleibt im Auslieferungszustand aus
    assert info["map_tile_url"].startswith("https://")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI nur unter Windows")
def test_reveal_credentials_returns_stored_values(bridge):
    """Gespeicherte Geheimnisse muessen wieder auslesbar sein - sonst kommt man nie mehr heran."""
    from zeitspur import dawarich, teams
    with pytest.raises(ValueError):
        bridge.reveal_plugin_credentials("teams")          # nichts hinterlegt
    with pytest.raises(ValueError):
        bridge.reveal_plugin_credentials("dawarich")

    teams.save_credentials("tenant-1", "client-1", "s3cr3t")
    dawarich.save_credentials("https://beispiel.invalid", "tok3n")
    assert bridge.reveal_plugin_credentials("teams") == {
        "tenant_id": "tenant-1", "client_id": "client-1", "client_secret": "s3cr3t"}
    assert bridge.reveal_plugin_credentials("dawarich") == {
        "base_url": "https://beispiel.invalid", "token": "tok3n"}
    teams.clear_credentials()
    dawarich.clear_credentials()


def test_release_ausgabe_ohne_karte_und_orte(bridge, monkeypatch):
    """Release-Paket ohne Standort-Historie: keine Karte, keine Orte, kein Import des fehlenden Moduls."""
    import json

    from zeitspur import edition
    monkeypatch.setattr(edition, "LOCATIONS", False)
    monkeypatch.setitem(sys.modules, "zeitspur.dawarich", None)   # Modul fehlt wie im Release-Paket
    bridge._app.cfg.map_enabled = True              # selbst eingeschaltet bleibt die Karte aus
    st = bridge.get_state()
    assert st["map_enabled"] is False and st["features"] == {"locations": False, "updates": False}
    assert bridge._places() == []
    html = build_html()
    assert "leafletjs.com" not in html and "/*__LEAFLET_JS__*/" not in html
    # Ein Standort-Ereignis aus einer frueheren Entwicklerversion darf nichts zum Absturz bringen
    ev = format_event({"id": 1, "source": "dawarich", "category": "visit", "ts_start": T0, "ts_end": T0 + 1000,
                       "subject": "Aufenthalt", "extra": json.dumps({"latitude": 48.1, "longitude": 11.5})},
                      places=["irgendein Ort"])
    assert ev["place"] is None and ev["subject"] == "Aufenthalt"


def test_entwicklerausgabe_meldet_ihre_funktionen(bridge):
    from zeitspur import edition
    assert bridge.get_state()["features"] == {"locations": edition.LOCATIONS, "updates": False}
