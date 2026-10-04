"""Plugin-System: Registry, Selbstbeschreibung, Migration alter Konfigurationen, Hinzufuegen/Entfernen."""
import pytest
import yaml

from zeitspur import plugins
from zeitspur.app import App
from zeitspur.config import Config, config_path, load_config, save_config
from zeitspur.outlook import CalendarEvent


def _ereignis(ext_id: str, ts: int) -> dict:
    return CalendarEvent(ext_id=ext_id, ts_start=ts, ts_end=ts + 60_000, subject=ext_id, location=None,
                         organizer=None, attendees=None, category="call", extra="{}").as_row()


# --------------------------------------------------------------------------- Registry

def test_plugin_ids_sind_stabil():
    """Die Id ist zugleich die source in calendar_events - eine Umbenennung liesse Daten verwaisen."""
    assert [p.id for p in plugins.PLUGINS] == [
        "outlook", "teams", "teams_local", "ics", "outlook_mail", "calls_local", "notifications",
        "browser_history", "git", "github", "pc_times", "wifi", "dawarich"]


def test_get_und_display():
    assert plugins.get("teams").name == "Microsoft Teams"
    with pytest.raises(plugins.UnknownPlugin):
        plugins.get("gibts-nicht")
    assert plugins.display("dawarich") == {"label": "Standort", "color": "#0f8a6a", "lane": "Orte"}
    assert plugins.display("fremd")["lane"] == "Termine"   # Unbekanntes faellt nicht aus dem Zeitstrahl


def test_installed_haelt_feste_reihenfolge():
    cfg = Config(installed_plugins=["dawarich", "outlook"])
    assert [p.id for p in plugins.installed(cfg)] == ["outlook", "dawarich"]


def test_describe_zeigt_probleme_nur_fuer_installierte(monkeypatch):
    monkeypatch.setattr("zeitspur.teams.has_credentials", lambda *a, **k: False)
    teams = plugins.get("teams")
    assert teams.describe(Config())["problem"] is None          # nicht hinzugefuegt: kein Alarm
    d = teams.describe(Config(installed_plugins=["teams"]))
    assert d["installed"] and "Zugangsdaten" in d["problem"]
    monkeypatch.setattr("zeitspur.teams.has_credentials", lambda *a, **k: True)
    assert "Identit" in teams.describe(Config(installed_plugins=["teams"]))["problem"]
    assert teams.describe(Config(installed_plugins=["teams"], teams_user_id="TIM"))["problem"] is None


def test_plugin_ohne_zugangsdaten_braucht_keine():
    outlook = plugins.get("outlook")
    assert outlook.credential_fields == () and outlook.has_credentials()
    assert outlook.not_ready_reason(Config()) is None


# --------------------------------------------------------------------------- Migration

def _schreibe(text: str) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(text, encoding="utf-8")


def test_migration_uebernimmt_was_lief(data_dir):
    _schreibe("outlook_enabled: true\nteams_enabled: true\ndawarich_enabled: true\n")
    assert load_config().installed_plugins == ["outlook", "teams", "dawarich"]


def test_migration_beachtet_die_alten_vorgaben(data_dir):
    # Outlook war frueher standardmaessig an - wer den Schalter nie angefasst hat, behaelt es.
    _schreibe("retention_days: 10\n")
    assert load_config().installed_plugins == ["outlook"]
    _schreibe("outlook_enabled: false\ndawarich_enabled: true\n")
    assert load_config().installed_plugins == ["dawarich"]


def test_neue_installation_hat_kein_plugin(data_dir):
    assert not config_path().exists()
    assert load_config().installed_plugins == []


def test_explizite_plugin_liste_gewinnt_gegen_alte_schalter(data_dir):
    _schreibe("installed_plugins: []\nteams_enabled: true\n")
    assert load_config().installed_plugins == []   # ein entferntes Plugin wird nicht wiederbelebt


def test_migration_wird_ohne_alte_schalter_gespeichert(data_dir):
    _schreibe("teams_enabled: true\noutlook_enabled: false\n")
    save_config(load_config())
    gespeichert = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    assert gespeichert["installed_plugins"] == ["teams"]
    assert "teams_enabled" not in gespeichert and "outlook_enabled" not in gespeichert


# --------------------------------------------------------------------------- Hinzufuegen / Entfernen
# Die echten App-Methoden an einem schlanken Traeger - eine vollstaendige App braucht Fenster und Threads.

class _Host:
    add_plugin = App.add_plugin
    remove_plugin = App.remove_plugin

    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.sync_angestossen = 0

    def _ensure_event_sync(self):
        self.sync_angestossen += 1


def test_hinzufuegen_speichert_dauerhaft_und_stoesst_sync_an(storage, data_dir):
    host = _Host(Config(), storage)
    host.add_plugin("dawarich")
    host.add_plugin("dawarich")        # doppelt hinzufuegen ist harmlos
    assert host.cfg.installed_plugins == ["dawarich"]
    assert load_config().installed_plugins == ["dawarich"]
    assert host.sync_angestossen == 2
    with pytest.raises(plugins.UnknownPlugin):
        host.add_plugin("gibts-nicht")


def test_entfernen_raeumt_zugangsdaten_ereignisse_und_cache_ab(storage, data_dir):
    from zeitspur import teams
    teams.save_credentials("tenant", "client", "geheim")
    storage.replace_events("teams", 0, 10 ** 13, [_ereignis("a", 1_000_000), _ereignis("b", 2_000_000)])
    storage.replace_events("dawarich", 0, 10 ** 13, [_ereignis("v", 3_000_000)])
    storage.teams_mark_seen([("a", True, 1_000_000)])
    host = _Host(Config(installed_plugins=["teams", "dawarich"]), storage)

    res = host.remove_plugin("teams")

    assert res == {"removed_events": 2}
    assert host.cfg.installed_plugins == ["dawarich"] and load_config().installed_plugins == ["dawarich"]
    assert not teams.has_credentials()                    # Geheimnis nicht liegen gelassen
    assert storage.teams_seen_map() == {}                 # Cache leer
    # Nur Teams ist weg - die Ereignisse der anderen Plugins bleiben
    assert [e["source"] for e in storage.events_between(0, 10 ** 13)] == ["dawarich"]


# --------------------------------------------------------------------------- Release-Ausgabe (ohne Standort)

def test_release_ausgabe_kennt_kein_standort_plugin():
    assert "dawarich" not in [p.id for p in plugins._registry(False)]
    assert [p.id for p in plugins._registry(True)][-1] == "dawarich"


def test_fehlendes_plugin_wird_nur_einmal_gemeldet(caplog, monkeypatch):
    """installed() laeuft im Beobachter alle paar Sekunden - die Warnung darf das Log nicht fluten."""
    monkeypatch.setattr(plugins, "_unknown_logged", set())
    cfg = Config(installed_plugins=["gibts-nicht"])
    with caplog.at_level("WARNING"):
        for _ in range(3):
            plugins.installed(cfg)
    assert sum("gibts-nicht" in r.getMessage() for r in caplog.records) == 1


def test_konfiguration_ohne_standort_funktionen(monkeypatch):
    """Release-Paket: Bekannte Orte bleiben ungeprueft liegen, statt dass das Laden scheitert."""
    import sys

    from zeitspur import edition
    monkeypatch.setattr(edition, "LOCATIONS", False)
    monkeypatch.setitem(sys.modules, "zeitspur.dawarich", None)   # Modul fehlt wie im Release-Paket
    Config(known_places=["kaputt", "Zuhause;48.1;11.5"]).validate()  # kein Fehler und kein Import
