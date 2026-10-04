"""Plugin-Grundlagen: Felder, Einstellungen, Zugangsdaten-Speicher, Selbstbeschreibung fuer den Plugin-Browser."""
import pytest

from zeitspur import credentials, plugins
from zeitspur.app import App
from zeitspur.config import Config, load_config


# --------------------------------------------------------------------------- Felder

def test_feldarten_wandeln_formularwerte():
    assert plugins.Field("a", "A", kind="list").coerce(" x \n\n y ") == ["x", "y"]
    assert plugins.Field("a", "A", kind="folders").coerce(["C:\\A", " "]) == ["C:\\A"]
    assert plugins.Field("a", "A", kind="bool").coerce("false") is False
    assert plugins.Field("a", "A", kind="bool", default=True).coerce(None) is True
    assert plugins.Field("a", "A", kind="number", default=5).coerce("") == 5
    assert plugins.Field("a", "A", kind="number").coerce("2,5") == 2.5
    assert plugins.Field("a", "A", kind="number").coerce("3") == 3
    assert plugins.Field("a", "A", kind="text").coerce("  x ") == "x"
    assert plugins.Field("a", "A", kind="secret").coerce(" geheim ") == " geheim "
    sel = plugins.Field("a", "A", kind="select", options=(("x", "X"), ("y", "Y")))
    assert sel.coerce("y") == "y"
    with pytest.raises(ValueError):
        sel.coerce("z")


# --------------------------------------------------------------------------- Selbstbeschreibung

@pytest.mark.parametrize("plugin", plugins._registry(True), ids=lambda p: p.id)
def test_jedes_plugin_beschreibt_sich_vollstaendig(plugin):
    assert plugin.category in plugins.CATEGORIES
    assert plugin.lane in plugins.LANES
    assert plugin.name and plugin.summary and plugin.description and plugin.records and plugin.setup_steps
    assert plugin.network in ("local", "online") and plugin.account in ("none", "token", "app")
    assert plugin.icon and plugin.color.startswith("#") and plugin.label
    assert all(f.kind in ("text", "secret", "secretlist", "list", "folders", "bool", "number", "select")
               for f in (*plugin.credential_fields, *plugin.setting_fields))


def test_kategorien_der_ereignisse_haben_anzeigenamen():
    for cat in ("conversation", "remote", "recording", "voice", "microphone", "notification", "web",
                "mail_sent", "mail_received", "commit", "github", "pc_on", "wifi"):
        assert plugins.CATEGORY_LABELS[cat]


def test_describe_liefert_alles_fuer_den_browser():
    d = plugins.get("browser_history").describe(Config())
    for key in ("summary", "category", "icon", "network", "account", "records", "privacy", "setup_steps",
                "settings", "requires_credentials"):
        assert key in d
    assert d["settings"] == {"exclude_domains": [], "store_titles": True}


# --------------------------------------------------------------------------- Einstellungen

def test_einstellungen_mit_vorgaben_und_gespeicherten_werten():
    git = plugins.get("git")
    assert git.settings(Config()) == {"folders": [], "authors": []}
    cfg = Config(plugin_settings={"git": {"folders": ["C:\\Projekte"]}})
    assert git.settings(cfg) == {"folders": ["C:\\Projekte"], "authors": []}
    assert "Ordner" in git.not_ready_reason(Config())          # Pflichtfeld fehlt


def test_einstellungen_werden_geprueft(tmp_path):
    git = plugins.get("git")
    with pytest.raises(ValueError, match="nicht gefunden"):
        git.clean_settings({"folders": str(tmp_path / "gibts-nicht")})
    assert git.clean_settings({"folders": str(tmp_path), "authors": "a@b.de\n"}) == {
        "folders": [str(tmp_path)], "authors": ["a@b.de"]}
    with pytest.raises(ValueError, match="Benutzername"):
        plugins.get("github").clean_settings({"username": "kein gültiger name"})


class _Host:
    save_plugin_settings = App.save_plugin_settings
    _apply_in_place = App._apply_in_place

    def __init__(self, cfg):
        self.cfg = cfg
        self.event_sync = None
        self.syncs = 0

    def _ensure_event_sync(self):
        self.syncs += 1


def test_einstellungen_speichern_dauerhaft(data_dir, tmp_path):
    host = _Host(Config(installed_plugins=["git"]))
    d = host.save_plugin_settings("git", {"folders": str(tmp_path), "authors": ""})
    assert d["settings"]["folders"] == [str(tmp_path)] and d["problem"] is None
    assert load_config().plugin_settings == {"git": {"folders": [str(tmp_path)], "authors": []}}
    assert host.syncs == 1


def test_teams_einstellungen_bleiben_eigene_schluessel(data_dir):
    host = _Host(Config(installed_plugins=["teams"]))
    host.save_plugin_settings("teams", {"teams_user_id": " abc ", "teams_user_names": "Max\nMaxi"})
    cfg = load_config()
    assert cfg.teams_user_id == "abc" and cfg.teams_user_names == ["Max", "Maxi"]
    assert "teams" not in cfg.plugin_settings


def test_ungueltige_plugin_einstellungen_scheitern_beim_laden():
    with pytest.raises(ValueError):
        Config(plugin_settings={"git": {"folders": [1, 2]}}).validate()
    with pytest.raises(ValueError):
        Config(plugin_settings={"git": "nein"}).validate()


# --------------------------------------------------------------------------- Zugangsdaten

def test_zugangsdaten_roundtrip_und_aufraeumen(data_dir):
    assert credentials.load("github") is None and not credentials.exists("github")
    p = credentials.save("github", {"token": "geheim"})
    assert p.parent == data_dir / "plugins" and b"geheim" not in p.read_bytes()
    assert credentials.load("github") == {"token": "geheim"}
    assert credentials.clear("github") and not credentials.clear("github")


def test_zugangsdaten_sind_an_das_plugin_gebunden(data_dir):
    """Eine Datei laesst sich keinem anderen Plugin unterschieben (Plugin-Id in der Zusatzentropie)."""
    credentials.save("github", {"token": "geheim"})
    credentials.path("ics").write_bytes(credentials.path("github").read_bytes())
    assert credentials.load("ics") is None


def test_ungueltige_plugin_id():
    with pytest.raises(ValueError):
        credentials.path("../boese")


def test_zugangsdaten_pruefung_optional_und_mehrzeilig(data_dir):
    github = plugins.get("github")
    assert github.clean_credentials({}) == {"token": ""}          # Token ist optional
    assert not github.requires_credentials and github.has_credentials() is False
    ics = plugins.get("ics")
    assert ics.requires_credentials
    with pytest.raises(ValueError, match="erforderlich"):
        ics.clean_credentials({"urls": ""})
    with pytest.raises(ValueError, match="Zeile 1") as err:
        ics.clean_credentials({"urls": "http://geheim.example.org/x.ics"})
    assert "geheim" not in str(err.value)                          # der Link erscheint nie in Meldungen
    clean = ics.clean_credentials({"urls": "Arbeit | webcal://kal.example.org/a.ics\n\n"})
    assert clean == {"urls": "Arbeit | webcal://kal.example.org/a.ics"}
    ics.save_credentials(clean)
    assert ics.has_credentials() and ics.reveal_credentials() == clean
    assert ics.clear_credentials()


# --------------------------------------------------------------------------- Ersteinrichtung

def test_ersteinrichtung_uebernimmt_nur_bekannte_verfuegbare_plugins(monkeypatch):
    from zeitspur.timeline_ui.bridge import Bridge

    captured = {}

    class FakeApp:
        cfg = Config()

        def complete_first_run(self, cfg):
            captured["cfg"] = cfg
            return True

    bridge = Bridge(FakeApp())
    monkeypatch.setattr(bridge, "get_state", lambda: {})
    monkeypatch.setattr(plugins.get("pc_times"), "unavailable_reason", lambda: "nicht hier")
    for pid in ("git", "wifi"):   # unabhaengig davon, was auf dem Test-PC installiert ist
        monkeypatch.setattr(plugins.get(pid), "unavailable_reason", lambda: None)
    bridge.complete_setup({"retention_days": "14",
                           "installed_plugins": ["git", "gibts-nicht", "pc_times", "git", "wifi"]})
    assert captured["cfg"].installed_plugins == ["git", "wifi"]
