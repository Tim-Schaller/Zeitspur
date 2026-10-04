"""Teams-Gespraeche ohne Entra-App: Mikrofon-Nutzung (Windows) + Fenstertitel."""
import json
import threading
from datetime import datetime, timezone

from zeitspur import plugins, teams_local
from zeitspur.config import Config
from zeitspur.events_sync import EventSync, PluginObserverThread
from zeitspur.teams_local import (
    MIN_SESSION_MS,
    SOURCE,
    CallRecorder,
    MicUsage,
    _usage_from_values,
    call_detail,
    filetime_to_ms,
)

START = 1_790_000_000_000      # ein fester Zeitpunkt in Unix-ms
S = 1000


# --------------------------------------------------------------------------- Windows-Werte deuten

def test_filetime_aus_der_registry():
    """Ein Registry-Wert (FILETIME, 100-ns-Schritte seit 1601) - hier der 02.03.2026, 08:15:00 UTC."""
    ms = filetime_to_ms(134169129004242000)
    assert datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") == "2026-03-02 08:15:00"


def test_laufende_und_beendete_nutzung():
    start_ft = 134169129004242000
    beendet = _usage_from_values("MSTeams", start_ft, start_ft + 230_000_000)   # 23 s spaeter
    assert not beendet.running and beendet.stop_ms - beendet.start_ms == 23_000
    assert _usage_from_values("MSTeams", start_ft, 0).running                 # Stop = 0: in Benutzung
    assert _usage_from_values("MSTeams", start_ft, start_ft - 10).running     # neue Nutzung nach alter
    assert _usage_from_values("MSTeams", None, 0) is None                     # nie benutzt


# --------------------------------------------------------------------------- Gegenueber aus Titeln

def test_gegenueber_nur_wenn_teams_es_ausdruecklich_zeigt():
    # Aufbau wie bei einem echten eingehenden Anruf
    anruf = ("Anruf von +49 000 1234567 im Auftrag von Zentrale | Beispiel GmbH | Beispiel GmbH | "
             "max.mustermann@example.com | Microsoft Teams")
    assert call_detail([anruf]) == "Anruf von +49 000 1234567 im Auftrag von Zentrale"
    assert call_detail(["Call with Bob | Microsoft Teams"]) == "Call with Bob"
    # eigenes Besprechungsfenster
    assert call_detail(["Projekt-Jour-Fixe | Microsoft Teams"]) == "Projekt-Jour-Fixe"
    # Das Hauptfenster zeigt, was man ansieht - nicht, mit wem man spricht
    haupt = "Chat | Muster, Erika | Beispiel GmbH | Beispiel GmbH | max.mustermann@example.com | Microsoft Teams"
    assert call_detail([haupt]) is None
    assert call_detail(["Kompakte Besprechungsansicht | Microsoft Teams", "Anrufe | Microsoft Teams"]) is None
    assert call_detail([]) is None


# --------------------------------------------------------------------------- Mitschreiben

class Quelle:
    """Steuerbare Mikrofon-Nutzung und Fenstertitel."""

    def __init__(self):
        self.usage: list[MicUsage] = []
        self.titles: list[str] = []
        self.titel_gelesen = 0

    def read_usage(self):
        return list(self.usage)

    def read_titles(self):
        self.titel_gelesen += 1
        return list(self.titles)


def _gespraeche(storage):
    return [dict(r, extra=json.loads(r["extra"])) for r in storage.events_between(0, 10 ** 14)
            if r["source"] == SOURCE]


def test_laufendes_gespraech_wird_fortgeschrieben_und_abgeschlossen(storage):
    q = Quelle()
    rec = CallRecorder(q.read_usage, q.read_titles)

    q.usage = [MicUsage("MSTeams", START, None)]
    assert rec.observe(storage, START + 3 * S) == 0          # nach 3 s: noch kein Gespraech
    assert _gespraeche(storage) == []

    q.titles = ["Anruf von Bob | Beispiel GmbH | Beispiel GmbH | x@example.com | Microsoft Teams"]
    assert rec.observe(storage, START + 10 * S) == 1
    q.titles = ["Chat | Bob | Beispiel GmbH | Beispiel GmbH | x@example.com | Microsoft Teams"]
    assert rec.observe(storage, START + 60 * S) == 1
    [g] = _gespraeche(storage)                                # eine Zeile, nicht eine je Blick
    assert g["ts_end"] == START + 60 * S and g["extra"]["in_progress"] is True
    assert g["subject"] == "Teams-Gespräch: Anruf von Bob"     # bleibt, auch wenn der Titel wechselt

    q.usage = [MicUsage("MSTeams", START, START + 75 * S)]   # aufgelegt
    assert rec.observe(storage, START + 80 * S) == 1
    [g] = _gespraeche(storage)
    assert g["ts_end"] == START + 75 * S and g["extra"]["in_progress"] is False   # genaue Endzeit
    assert g["subject"] == "Teams-Gespräch: Anruf von Bob" and g["attendees"] == "Anruf von Bob"

    assert rec.observe(storage, START + 90 * S) == 0          # danach nicht mehr angefasst


def test_neustart_mitten_im_gespraech(storage):
    """Zeitspur wird waehrend eines Anrufs neu gestartet: weiterfuehren, nichts verdoppeln."""
    q = Quelle()
    q.usage = [MicUsage("MSTeams", START, None)]
    q.titles = ["Anruf von Bob | Microsoft Teams"]
    CallRecorder(q.read_usage, q.read_titles).observe(storage, START + 30 * S)

    q.titles = []                                             # neuer Prozess sieht den Titel nicht mehr
    neu = CallRecorder(q.read_usage, q.read_titles)
    neu.observe(storage, START + 50 * S)
    q.usage = [MicUsage("MSTeams", START, START + 55 * S)]
    neu.observe(storage, START + 60 * S)
    [g] = _gespraeche(storage)
    assert g["ts_end"] == START + 55 * S and g["subject"] == "Teams-Gespräch: Anruf von Bob"


def test_neustart_nach_dem_gespraech_aendert_nichts(storage):
    q = Quelle()
    q.usage = [MicUsage("MSTeams", START, START + 40 * S)]
    assert CallRecorder(q.read_usage, q.read_titles).observe(storage, START + 50 * S) == 1
    vorher = _gespraeche(storage)
    assert CallRecorder(q.read_usage, q.read_titles).observe(storage, START + 99 * S) == 0
    assert _gespraeche(storage) == vorher


def test_gespraech_waehrend_zeitspur_aus_war(storage):
    """Nur noch die letzte Nutzung ist bekannt - die Zeiten stimmen, ein Gegenueber gibt es nicht."""
    q = Quelle()
    q.usage = [MicUsage("MSTeams", START, START + 120 * S)]
    CallRecorder(q.read_usage, q.read_titles).observe(storage, START + 999 * S)
    [g] = _gespraeche(storage)
    assert (g["ts_start"], g["ts_end"]) == (START, START + 120 * S)
    assert g["subject"] == "Teams-Gespräch" and q.titel_gelesen == 0   # beendet: keine Titel mehr lesen


def test_kurze_nutzung_ist_kein_gespraech(storage):
    q = Quelle()
    q.usage = [MicUsage("MSTeams", START, START + MIN_SESSION_MS - 1)]
    assert CallRecorder(q.read_usage, q.read_titles).observe(storage, START + 60 * S) == 0
    assert _gespraeche(storage) == []


def test_bei_pausierter_aufnahme_keine_fenstertitel(storage):
    q = Quelle()
    q.usage = [MicUsage("MSTeams", START, None)]
    q.titles = ["Anruf von Bob | Microsoft Teams"]
    CallRecorder(q.read_usage, q.read_titles).observe(storage, START + 30 * S, titles_allowed=False)
    [g] = _gespraeche(storage)
    assert q.titel_gelesen == 0 and g["subject"] == "Teams-Gespräch" and "window_titles" not in g["extra"]


# --------------------------------------------------------------------------- Plugin-System

def test_plugin_ist_beobachtend_und_ohne_zugangsdaten():
    p = plugins.get("teams_local")
    assert p.observes and p.credential_fields == () and p.setting_fields == ()
    assert p.describe(Config())["observes"] is True
    assert plugins.display("teams_local")["label"] == "Teams (lokal)"


def test_abruf_synchronisierung_laesst_beobachtende_plugins_aus(storage):
    sync = EventSync(Config(installed_plugins=["teams_local"]), storage)
    assert not sync.any_enabled()
    report = sync.sync_range(datetime(2026, 10, 1).date(), datetime(2026, 10, 1).date())
    assert report.total == 0 and not report.errors   # kein "fetch nicht implementiert"


def test_beobachter_takt(storage, monkeypatch):
    q = Quelle()
    q.usage = [MicUsage("MSTeams", START, None)]
    q.titles = ["Anruf von Bob | Microsoft Teams"]
    plugin = plugins.get("teams_local")
    monkeypatch.setattr(plugin, "_recorder", CallRecorder(q.read_usage, q.read_titles))
    monkeypatch.setattr(teams_local, "mic_access_reason", lambda: None)
    monkeypatch.setattr("zeitspur.timeutil.now_ms", lambda: START + 30 * S)
    pausiert = {"ja": False}
    cfg = Config(installed_plugins=[])
    obs = PluginObserverThread(cfg, storage, threading.Event(), paused=lambda: pausiert["ja"])

    assert obs.tick() == 0                                    # nicht hinzugefuegt: nichts
    cfg.installed_plugins = ["teams_local"]                   # zur Laufzeit hinzugefuegt
    assert obs.tick() == 1 and q.titel_gelesen == 1
    pausiert["ja"] = True
    obs.tick()
    assert q.titel_gelesen == 1                               # pausiert: Titel nicht gelesen


def test_beobachter_ueberspringt_gesperrtes_mikrofon(storage, monkeypatch):
    plugin = plugins.get("teams_local")
    rec = CallRecorder(lambda: [MicUsage("MSTeams", START, None)], lambda: [])
    monkeypatch.setattr(plugin, "_recorder", rec)
    monkeypatch.setattr(teams_local, "mic_access_reason", lambda: "gesperrt")
    obs = PluginObserverThread(Config(installed_plugins=["teams_local"]), storage, threading.Event())
    assert obs.tick() == 0


def test_beobachter_ueberlebt_fehler_im_plugin(storage, monkeypatch):
    plugin = plugins.get("teams_local")

    def kaputt():
        raise RuntimeError("Registry weg")

    monkeypatch.setattr(plugin, "_recorder", CallRecorder(kaputt, lambda: []))
    monkeypatch.setattr(teams_local, "mic_access_reason", lambda: None)
    obs = PluginObserverThread(Config(installed_plugins=["teams_local"]), storage, threading.Event())
    assert obs.tick() == 0 and obs.tick() == 0                # kein Absturz, auch beim zweiten Mal
