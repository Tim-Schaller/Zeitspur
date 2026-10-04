"""Plugin "Windows-Standort", Punkte-Tabelle, "Ort benennen" und die Standort-Spur im Zeitstrahl.

Die Ortung von Windows selbst wird nicht angefasst: Tests arbeiten mit einer Attrappe des Samplers.
Alle Orte und Koordinaten sind erfunden.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

import pytest

from zeitspur import location, plugins, timeutil, windows_location
from zeitspur.config import Config, load_config
from zeitspur.location import Point
from zeitspur.windows_location import Fix, LocationRecorder

DAY = datetime(2026, 9, 9)
BUERO = (52.50000, 13.35000)
ZUHAUSE = (52.52000, 13.40500)
MIN = 60_000


def T(h, m=0, s=0):  # noqa: N802
    return timeutil.to_ms(DAY.replace(hour=h, minute=m, second=s))


class FakeSampler:
    def __init__(self, fix=None):
        self.fix = fix
        self.reads = 0
        self.restarts = 0

    def read(self):
        self.reads += 1
        return self.fix

    def wait_for_fix(self, timeout_s=10.0):
        return self.fix

    def restart(self):
        self.restarts += 1


# --------------------------------------------------------------------------- Messen

def test_misst_erst_nach_dem_einpendeln_und_dann_im_takt(storage):
    s = FakeSampler(Fix(T(8), *BUERO, 20.0))
    rec = LocationRecorder(s)
    assert rec.observe(storage, T(8), interval_ms=5 * MIN) == 0          # erster Blick: Windows ortet neu
    assert rec.observe(storage, T(8, 0, 30), interval_ms=5 * MIN) == 0   # noch nicht eingependelt
    assert rec.observe(storage, T(8, 1), interval_ms=5 * MIN) == 1       # erste Messung -> ein Aufenthalt
    rec.observe(storage, T(8, 2), interval_ms=5 * MIN)                   # zu frueh fuer die naechste
    assert s.reads == 1
    for minute in range(3, 30):
        rec.observe(storage, T(8, minute), interval_ms=5 * MIN)
    assert len(storage.location_points("windows_location", T(0), T(23))) == 6
    rows = storage.events_between(T(0), T(23), sources=["windows_location"])
    assert len(rows) == 1 and (rows[0]["ts_start"], rows[0]["ts_end"]) == (T(8, 1), T(8, 26))
    assert json.loads(rows[0]["extra"])["accuracy_m"] == 20


def test_pause_und_standby(storage):
    s = FakeSampler(Fix(T(8), *BUERO, 20.0))
    rec = LocationRecorder(s)
    rec.observe(storage, T(8), interval_ms=5 * MIN)
    assert rec.observe(storage, T(8, 2), interval_ms=5 * MIN, allowed=False) == 0   # Aufnahme pausiert
    assert s.reads == 0
    rec.observe(storage, T(8, 3), interval_ms=5 * MIN)
    assert s.reads == 1
    # 40 Minuten nichts: der PC hat geschlafen -> Ortung neu starten, erst nach dem Einpendeln messen
    rec.observe(storage, T(8, 43), interval_ms=5 * MIN)
    assert s.restarts == 1 and s.reads == 1
    rec.observe(storage, T(8, 44), interval_ms=5 * MIN)
    assert s.reads == 2


def test_ungenaue_oder_fehlende_ortung_wird_verworfen(storage):
    for fix in (None, Fix(T(8), *BUERO, 3000.0)):
        rec = LocationRecorder(FakeSampler(fix))
        rec.observe(storage, T(8), interval_ms=MIN)
        assert rec.observe(storage, T(8, 2), interval_ms=MIN) == 0
    assert storage.location_points("windows_location", T(0), T(23)) == []


def test_aufenthalte_aus_pc_messungen():
    pts = [Point(T(8, m), *BUERO, 30.0) for m in range(0, 60, 5)]
    pts += [Point(T(13, m), *BUERO, 30.0) for m in range(0, 30, 5)]        # Standby ueber Mittag
    pts += [Point(T(14, 30), *ZUHAUSE, 25.0)]                                # einmal kurz zu Hause an
    rows = windows_location.stays_from_points(pts)
    assert [(timeutil.fmt_hm(r["ts_start"]), timeutil.fmt_hm(r["ts_end"])) for r in rows] == [
        ("08:00", "08:55"), ("13:00", "13:25"), ("14:30", "14:30")]
    # in der Standort-Spur: Buero durchgehend (Mittag erschlossen), dann Fahrt nach Hause
    events = [dict(r, source="windows_location") for r in rows]
    segs = location.strip_for_range(events, T(8), T(14, 31), now=T(14, 31),
                                    places=[location.KnownPlace("Büro", *BUERO), location.KnownPlace("Zuhause", *ZUHAUSE)])
    assert [(s.kind, s.name) for s in segs] == [("stay", "Büro"), ("trip", None), ("stay", "Zuhause")]
    assert segs[0].inferred == [(T(8, 55), T(13))]


def test_windows_erlaubt_den_zugriff_nicht(monkeypatch):
    import winreg

    values = {}
    monkeypatch.setattr(windows_location, "_consent",
                        lambda hive, sub: values.get((hive, sub.rsplit("\\", 1)[-1])))
    assert windows_location.access_reason() is None
    values[(winreg.HKEY_CURRENT_USER, "location")] = "Deny"
    assert "Ortungsdienste" in windows_location.access_reason()
    values[(winreg.HKEY_CURRENT_USER, "location")] = "Allow"
    values[(winreg.HKEY_CURRENT_USER, "NonPackaged")] = "Deny"
    assert "Desktop-Apps" in windows_location.access_reason()
    values[(winreg.HKEY_LOCAL_MACHINE, "location")] = "Deny"
    assert "Administrator" in windows_location.access_reason()


def test_plugin_beschreibung_und_einstellungen():
    p = plugins.get("windows_location")
    assert p.observes and p.lane == "Orte" and p.category == "Orte"
    assert p.clean_settings({"interval_min": "10"}) == {"interval_min": 10}
    with pytest.raises(ValueError, match="1 bis 60"):
        p.clean_settings({"interval_min": "0"})
    assert "Microsoft" in p.privacy


def test_plugin_entfernen_loescht_die_punkte(storage):
    storage.add_location_point("windows_location", T(8), *BUERO, 20.0)
    storage.add_location_point("dawarich", T(8), *BUERO, 5.0)
    plugins.get("windows_location").on_remove(storage)
    assert storage.location_points("windows_location", T(0), T(23)) == []
    assert len(storage.location_points("dawarich", T(0), T(23))) == 1
    storage.set_meta("dawarich.points_days", '{"2026-09-09": 1}')
    plugins.get("dawarich").on_remove(storage)
    assert storage.location_points("dawarich", T(0), T(23)) == []
    assert storage.get_meta("dawarich.points_days") == "{}"


# --------------------------------------------------------------------------- Punkte-Tabelle

def test_punkte_ersetzen_und_aufraeumen(storage):
    storage.replace_location_points("dawarich", T(0), T(12), [(T(8), 52.5, 13.35, 5.0), (T(9), 52.5, 13.35, None),
                                                             (T(13), 52.5, 13.35, None)])   # ausserhalb: verworfen
    assert [r["ts"] for r in storage.location_points("dawarich", T(0), T(23))] == [T(8), T(9)]
    storage.replace_location_points("dawarich", T(8, 30), T(12), [(T(10), 52.6, 13.4, 3.0)])
    assert [r["ts"] for r in storage.location_points("dawarich", T(0), T(23))] == [T(8), T(10)]
    assert storage.delete_location_points_older_than(T(9)) == 1
    assert [r["ts"] for r in storage.location_points("dawarich", T(0), T(23))] == [T(10)]


def test_aufbewahrungsfrist_gilt_auch_fuer_standortpunkte(storage):
    from zeitspur.cleanup import run_cleanup

    now = timeutil.now_ms()
    storage.add_location_point("dawarich", now - 30 * 86_400_000, 52.5, 13.35)
    storage.add_location_point("dawarich", now - 3_600_000, 52.5, 13.35)
    run_cleanup(storage, Config(retention_days=14), threading.Event())
    assert [r["ts"] for r in storage.location_points("dawarich", 0, now)] == [now - 3_600_000]


# --------------------------------------------------------------------------- Ort benennen

def _app(cfg: Config):
    from zeitspur.app import App

    app = App.__new__(App)   # nur, was name_place braucht
    app.cfg = cfg
    return app


def test_ort_benennen_legt_bekannten_ort_an_und_benennt_um():
    app = _app(Config(known_places=["Büro;52.5;13.35;200"]))
    app.name_place("Kunde Beispiel", lat=52.54, lon=13.45)
    assert app.cfg.known_places[-1] == "Kunde Beispiel;52.540000;13.450000;150"
    app.name_place("Hauptbüro", old_name="Büro", lat=52.5, lon=13.35)
    assert app.cfg.known_places[0] == "Hauptbüro;52.500000;13.350000;200"   # Radius bleibt
    assert load_config().known_places == app.cfg.known_places                # sofort gespeichert
    with pytest.raises(ValueError):
        app.name_place("A;B", lat=1, lon=2)


def test_ort_benennen_ueber_das_wlan():
    app = _app(Config(plugin_settings={"wifi": {"places": ["Gastnetz = Alt", "Heimnetz = Zuhause"]}}))
    app.name_place("Kunde Beispiel", ssid="gast-netz", old_name="Alt")
    assert app.cfg.plugin_settings["wifi"]["places"] == ["Gastnetz = Kunde Beispiel", "Heimnetz = Zuhause"]
    app.name_place("Kunde Muster", ssid="Neues Netz")                         # unbenannt: neue Zuordnung
    assert app.cfg.plugin_settings["wifi"]["places"][-1] == "Neues Netz = Kunde Muster"
    with pytest.raises(ValueError, match="="):
        app.name_place("X", ssid="a=b")
    with pytest.raises(ValueError, match="weder Koordinaten noch ein WLAN"):
        app.name_place("Irgendwas")


def test_kartenstartpunkt_umbenennen():
    app = _app(Config(map_home_label="Beispiel GmbH", map_home_lat=52.5, map_home_lon=13.35))
    app.name_place("Beispiel", old_name="Beispiel GmbH", lat=52.5, lon=13.35)
    assert app.cfg.map_home_label == "Beispiel" and app.cfg.known_places == []


def test_umbenennen_aendert_den_namen_ueberall():
    """Ein Ort darf nach dem Umbenennen nicht unter zwei Namen weiterleben (WLAN alt, bekannter Ort neu)."""
    app = _app(Config(known_places=["Zuhause;52.52;13.405"],
                      plugin_settings={"wifi": {"places": ["Heimnetz = Zuhause", "Firmennetz = Firma"]}}))
    app.name_place("Daheim", old_name="Zuhause", lat=52.52, lon=13.405)
    assert app.cfg.known_places == ["Daheim;52.520000;13.405000;150"]
    assert app.cfg.plugin_settings["wifi"]["places"] == ["Heimnetz = Daheim", "Firmennetz = Firma"]
    # Name nur aus dem WLAN, Koordinaten aus GPS: umbenannt wird die Zuordnung, kein zweiter Ort entsteht
    app.name_place("Büro", old_name="Firma", lat=52.5, lon=13.35)
    assert app.cfg.plugin_settings["wifi"]["places"][-1] == "Firmennetz = Büro"
    assert len(app.cfg.known_places) == 1


def test_von_zwei_orten_gleichen_namens_wird_nur_der_richtige_umbenannt():
    app = _app(Config(known_places=["Filiale;52.50;13.35", "Filiale;52.54;13.45"]))
    app.name_place("Filiale Nord", old_name="Filiale", lat=52.5401, lon=13.4501)
    assert app.cfg.known_places == ["Filiale;52.50;13.35", "Filiale Nord;52.540000;13.450000;150"]



def test_lange_aufenthalte_ueberleben_das_neuberechnen(storage):
    """96 Stunden am selben Ort: Jede Neuberechnung (36 h zurueck) darf den Anfang nicht abschneiden."""
    rec = LocationRecorder(FakeSampler(Fix(T(8), *BUERO, 20.0)))
    for k in range(0, 96 * 12 + 1):          # alle 5 Minuten gemessen, jede Stunde neu berechnet
        now = T(8) + k * 5 * MIN
        storage.add_location_point("windows_location", now, *BUERO, 20.0)
        if k % 12 == 0:
            rec.recompute(storage, now)
    rows = storage.events_between(T(0), T(8) + 97 * 3_600_000, sources=["windows_location"])
    assert [(r["ts_start"], r["ts_end"]) for r in rows] == [(T(8), T(8) + 96 * 3_600_000)]


def test_entfernen_beendet_die_ortung(storage):
    p = plugins.get("windows_location")
    sampler = FakeSampler(Fix(T(8), *BUERO, 20.0))
    sampler.close = lambda: setattr(sampler, "closed", True)
    rec = p._rec()
    rec.sampler = sampler
    p.on_remove(storage)
    assert sampler.closed and rec.closed
    assert rec.observe(storage, T(9), interval_ms=MIN) == 0 and p._recorder is None
