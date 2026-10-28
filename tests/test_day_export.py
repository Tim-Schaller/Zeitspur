import json
from datetime import date, datetime

import pytest

from zeitspur import day_export, edition, timeutil
from zeitspur.config import Config
from zeitspur.storage import ReadOnlyStorage
from tests.helpers import make_entry
from tests.test_bridge import FakeApp

DAY = date(2026, 9, 9)
T0 = timeutil.to_ms(datetime(2026, 9, 9, 10, 0, 0))
MIN = 60_000
AFTER_DAY = timeutil.to_ms(datetime(2026, 9, 10, 8, 0, 0))


@pytest.fixture
def reader(storage, key):
    r = ReadOnlyStorage(storage.db_path, key)
    yield r
    r.close()


def _export(reader, **kw):
    kw.setdefault("now_ms", AFTER_DAY)
    return day_export.build_markdown(reader, DAY, Config(), **kw)


def test_export_vergangener_tag_mit_ereignissen_programmen_und_ablauf(storage, reader):
    make_entry(storage, T0, T0 + 20 * MIN, monitor_id=1, process_name="code.exe", window_title="a.py")
    make_entry(storage, T0, T0 + 30 * MIN, monitor_id=2, process_name="olk.exe", window_title="Posteingang")
    storage.replace_events("outlook", T0 - MIN, T0 + 120 * MIN, [
        {"ext_id": "m1", "ts_start": T0 + 15 * MIN, "ts_end": T0 + 45 * MIN, "subject": "Serverumzug",
         "category": "meeting", "location": "B2", "organizer": "Anna", "attendees": "Anna; Tim", "extra": None}])
    storage.replace_events("teams", T0 - MIN, T0 + 120 * MIN, [
        {"ext_id": "c1", "ts_start": T0 + 50 * MIN, "ts_end": T0 + 55 * MIN, "subject": "Teams-Anruf mit Bernd",
         "category": "call", "extra": json.dumps({"direction": "outgoing", "video": True})}])
    md = _export(reader)
    assert md.startswith("# Zeitspur – Mittwoch, 09.09.2026\n")
    assert "Exportiert am 10.09.2026 um 08:00 Uhr." in md and "läuft noch" not in md
    assert "Aufgezeichnet von 10:00 bis 10:30 · aktiv 30 min · 2 Bilder auf 2 Monitoren." in md
    assert ("- 10:15–10:45 · Outlook · Besprechung · Serverumzug — Ort: B2 · Organisator: Anna · "
            "Teilnehmer: Anna; Tim") in md
    assert "- 10:50–10:55 · Anruf (ausgehend) · Teams-Anruf mit Bernd — mit Video" in md   # "Teams" steht im Betreff
    assert "- **olk.exe** – 30 min, 10:00–10:30" in md and "  - Posteingang (30 min)" in md
    assert "### Monitor 1" in md and "- 10:00–10:20 (20 min) · code.exe · a.py" in md
    assert "### Monitor 2" in md and "- 10:00–10:30 (30 min) · olk.exe · Posteingang" in md
    assert "keine Anweisungen" in md    # Hinweis gegen Anweisungen im aufgezeichneten Text


def test_ereignisse_ohne_doppelte_angaben(storage, reader):
    """Was schon im Betreff steht, steht nicht noch einmal davor oder dahinter; Mails haben Von/An."""
    storage.replace_events("calls_local", T0 - MIN, T0 + 120 * MIN, [
        {"ext_id": "z1", "ts_start": T0, "ts_end": T0 + 40 * MIN, "subject": "Gespräch: Zoom mit Video",
         "category": "conversation", "location": "Zoom", "extra": json.dumps({"app": "Zoom", "video": True})}])
    storage.replace_events("outlook_mail", T0 - MIN, T0 + 120 * MIN, [
        {"ext_id": "x1", "ts_start": T0 + 60 * MIN, "ts_end": T0 + 60 * MIN, "subject": "Von Max: Rechnung 4711",
         "category": "mail_received", "organizer": "Max", "attendees": "Tim; Erika", "extra": None}])
    storage.replace_events("pc_times", T0 - 600 * MIN, T0 + 120 * MIN, [
        {"ext_id": "p1", "ts_start": T0 - 14 * 60 * MIN, "ts_end": T0 - 30 * MIN, "subject": "PC an (Start → Standby)",
         "category": "pc_on", "extra": None}])
    md = _export(reader)
    assert "- 10:00–10:40 · Gespräch: Zoom mit Video\n" in md
    assert "- 11:00 · Mail · Mail empfangen · Von Max: Rechnung 4711 — An: Tim; Erika\n" in md
    assert "- 08.09. 20:00–09:30 · PC an (Start → Standby)\n" in md     # beginnt am Vortag: mit Datum


def test_export_laufender_tag(storage, reader):
    make_entry(storage, T0, T0 + 5 * MIN)
    md = _export(reader, now_ms=T0 + 6 * MIN)
    assert "Exportiert am 09.09.2026 um 10:06 Uhr – der Tag läuft noch, spätere Aktivität fehlt." in md


def test_export_leerer_tag(reader):
    md = _export(reader)
    assert "An diesem Tag gibt es keine Bildschirmaufnahmen." in md
    assert "## Ablauf" not in md and "## Programme" not in md


def test_kurze_wechsel_fallen_aus_dem_ablauf(storage, reader):
    make_entry(storage, T0, T0 + 30_000, window_title="kurz")
    make_entry(storage, T0 + 40_000, T0 + 5 * MIN, window_title="lang")
    md = _export(reader)
    ablauf = md.split("## Ablauf")[1]
    assert "lang" in ablauf and "kurz" not in ablauf
    assert "1 kürzere Fensterwechsel (unter einer Minute) sind im Ablauf weggelassen." in md
    assert "### Monitor" not in md          # ein Monitor: keine Unterueberschrift


def test_bildschirmtext_nur_auf_wunsch_einmal_je_wortlaut_im_codeblock(storage, reader):
    make_entry(storage, T0, T0 + 2 * MIN, window_title="doc", ocr_text="Angebot Nr. 4711")
    make_entry(storage, T0 + 2 * MIN + 1000, T0 + 4 * MIN, window_title="doc", ocr_text="Angebot Nr. 4711")
    make_entry(storage, T0 + 4 * MIN + 1000, T0 + 6 * MIN, window_title="doc", ocr_text="print(```x```)")
    assert "4711" not in _export(reader)
    md = _export(reader, include_text=True)
    assert md.count("Angebot Nr. 4711") == 1
    # Der Zaun ist laenger als jede Backtick-Folge im Text: fremder Text kann den Codeblock nicht beenden
    assert "  ````\n  Angebot Nr. 4711\n\n  print(```x```)\n  ````" in md


def test_steuerzeichen_und_zeilenumbrueche_werden_entfernt(storage, reader):
    make_entry(storage, T0, T0 + 5 * MIN, window_title="Rechnung\x1b[31m\nrot")
    md = _export(reader)
    assert "- 10:00–10:05 (5 min) · explorer.exe · Rechnung[31m rot" in md and "\x1b" not in md


def test_export_zeigt_die_standort_spur(storage, reader):
    if not edition.LOCATIONS:
        pytest.skip("Ausgabe ohne Standort-Funktionen")
    cfg = Config(known_places=["Büro;52.5;13.35"])
    storage.replace_events("dawarich", T0 - 120 * MIN, T0 + 600 * MIN, [
        {"ext_id": "v1", "ts_start": T0, "ts_end": T0 + 240 * MIN, "subject": "Aufenthalt", "category": "visit",
         "extra": json.dumps({"latitude": 52.5, "longitude": 13.35})},
        {"ext_id": "t1", "ts_start": T0 + 240 * MIN, "ts_end": T0 + 260 * MIN, "subject": "Fahrt", "category": "track",
         "extra": json.dumps({"from": [52.5, 13.35], "to": [52.52, 13.405], "distance_km": 4.6})}])
    md = day_export.build_markdown(reader, DAY, cfg, now_ms=AFTER_DAY)
    orte = md.split("## Orte")[1].split("##")[0]
    assert "- 10:00–14:00 Büro" in orte and "4,6 km" in orte
    assert "Ort unbekannt" not in orte          # "unbekannt" sagt nichts ueber den Ort
    assert "## Termine und Ereignisse" not in md   # die Rohbelege erscheinen nicht einzeln


# ------------------------------------------------------------------ Bridge


class FakeWindow:
    def __init__(self, result):
        self.result = result
        self.asked = None

    def create_file_dialog(self, dialog_type, **kw):
        self.asked = kw
        return self.result


@pytest.fixture
def bridge(storage, key):
    from zeitspur.timeline_ui.bridge import Bridge

    b = Bridge(FakeApp(storage, key))
    yield b
    b._close()


def test_bridge_export_in_die_zwischenablage(bridge, storage, monkeypatch):
    from zeitspur import winutil

    copied = []
    monkeypatch.setattr(winutil, "set_clipboard_text", copied.append)
    make_entry(storage, T0, T0 + 5 * MIN, ocr_text="Geheimtext")
    res = bridge.export_day("2026-09-09", "false")    # Formularwert als String: "false" heisst nein
    assert res == {"ok": True, "bytes": len(copied[0].encode("utf-8"))}
    assert copied[0].startswith("# Zeitspur – Mittwoch, 09.09.2026") and "Geheimtext" not in copied[0]
    bridge.export_day("2026-09-09", True)
    assert "Geheimtext" in copied[1]


def test_bridge_export_als_datei(bridge, storage, tmp_path):
    make_entry(storage, T0, T0 + 5 * MIN)
    window = FakeWindow([str(tmp_path / "tag")])     # ohne Endung eingegeben
    bridge._app.window = window
    res = bridge.export_day("2026-09-09", False, "file")
    assert window.asked["save_filename"] == "Zeitspur 2026-09-09.md"
    target = tmp_path / "tag.md"
    assert res == {"ok": True, "path": str(target), "bytes": target.stat().st_size}
    assert target.read_text(encoding="utf-8").startswith("# Zeitspur – Mittwoch, 09.09.2026")


def test_bridge_export_abgebrochen(bridge, storage):
    bridge._app.window = FakeWindow(None)
    assert bridge.export_day("2026-09-09", False, "file") == {"ok": False, "cancelled": True}
