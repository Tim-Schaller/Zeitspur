"""Automatische Updates: nur korrekt signierte, neuere Versionen werden geladen - und erst im Leerlauf
oder auf Knopfdruck installiert. Die Update-Quelle ist hier ein Ordner (file://), signiert mit einem
Testschluessel; das Setup wird nie wirklich gestartet."""
import base64
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zeitspur import edition, updater
from zeitspur.config import Config

SETUP = b"MZ ... so tut ein Setup in diesem Test nur so ... " * 50


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


def _pub(key) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _channel(tmp_path: Path, key, version="0.9.0", content=SETUP, **override) -> str:
    """Legt latest.json und das Setup in einen Ordner und liefert die file://-Adresse des Manifests."""
    folder = tmp_path / "channel"
    folder.mkdir(exist_ok=True)
    name = f"ZeitspurSetup-{version}.exe"
    (folder / name).write_bytes(content)
    payload = {"version": version, "file": name, "sha256": hashlib.sha256(content).hexdigest(),
               "size": len(content), "notes": "- Neu: Startfenster", **override}
    doc = {"payload": payload, "signature": updater.sign_payload(payload, key)}
    (folder / "latest.json").write_text(json.dumps(doc), encoding="utf-8")
    return (folder / "latest.json").as_uri()


def _updater(tmp_path, key, url, *, auto=True, idle=lambda: 0.0, current="0.2.0", launch=None):
    cfg = Config()
    cfg.auto_update = auto
    launched, messages = [], []
    u = updater.Updater(cfg, tmp_path / "data", threading.Event(), notify=messages.append, manifest_url=url,
                        public_key=_pub(key), current=current, idle_seconds=idle,
                        launch=launch or (lambda setup, log: launched.append(setup)), timing=(0.0, 3600.0, 300.0))
    return u, launched, messages


def _updates(tmp_path) -> Path:
    return tmp_path / "data" / "updates"


# ---- Versionen und Manifest ------------------------------------------------------------------------

def test_version_comparison():
    assert updater.is_newer("0.3.0", "0.2.0")
    assert updater.is_newer("0.10.0", "0.9.9")       # numerisch, nicht alphabetisch
    assert updater.is_newer("0.2.0.1", "0.2.0")      # vierstellige Testversionen
    assert not updater.is_newer("0.2.0", "0.2.0")
    assert not updater.is_newer("0.1.9", "0.2.0")
    with pytest.raises(updater.UpdateError):
        updater.parse_version("0.3.0-beta")


def test_manifest_signature_is_required(tmp_path, key):
    _channel(tmp_path, key)
    raw = (tmp_path / "channel" / "latest.json").read_bytes()
    assert updater.verify_manifest(raw, _pub(key))["version"] == "0.9.0"
    with pytest.raises(updater.UpdateError):        # fremder Schluessel
        updater.verify_manifest(raw, _pub(Ed25519PrivateKey.generate()))
    doc = json.loads(raw)
    doc["payload"]["version"] = "9.9.9"               # nachtraeglich veraendert
    with pytest.raises(updater.UpdateError):
        updater.verify_manifest(json.dumps(doc).encode(), _pub(key))
    with pytest.raises(updater.UpdateError):
        updater.verify_manifest(b"<html>Rate limit</html>", _pub(key))


@pytest.mark.parametrize("override", [
    {"file": "..\\..\\Startup\\evil.exe"},            # kein Pfad im Dateinamen
    {"file": "setup.msi"},
    {"sha256": "abc"},
    {"size": True},
    {"size": updater.MAX_SETUP_BYTES + 1},
    {"url": "http://example.org/setup.exe"},          # nur https
    {"notes": "x" * (updater.MAX_NOTES_CHARS + 1)},
])
def test_manifest_fields_are_checked_even_when_signed(tmp_path, key, override):
    _channel(tmp_path, key, **override)
    with pytest.raises(updater.UpdateError):
        updater.verify_manifest((tmp_path / "channel" / "latest.json").read_bytes(), _pub(key))


def test_download_with_wrong_checksum_leaves_nothing(tmp_path, key):
    url = _channel(tmp_path, key)
    dest = tmp_path / "dl" / "ZeitspurSetup-0.9.0.exe"
    with pytest.raises(updater.UpdateError):
        updater.download(url.replace("latest.json", dest.name), dest, "0" * 64, len(SETUP))
    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()


# ---- Ablauf ----------------------------------------------------------------------------------------

def test_auto_update_waits_until_the_pc_is_idle(tmp_path, key):
    idle = [0.0]
    u, launched, messages = _updater(tmp_path, key, _channel(tmp_path, key), idle=lambda: idle[0])
    u.step(check_due=True)
    state = u.snapshot()
    assert state["status"] == "ready" and state["version"] == "0.9.0" and state["notes"]
    assert (_updates(tmp_path) / "ZeitspurSetup-0.9.0.exe").read_bytes() == SETUP
    assert launched == []                              # geladen, aber der PC wird gerade benutzt
    assert any("0.9.0" in m for m in messages)
    idle[0] = 301.0
    u.step()
    assert [p.name for p in launched] == ["ZeitspurSetup-0.9.0.exe"]
    assert u.snapshot()["status"] == "installing"
    pending = json.loads((_updates(tmp_path) / "pending.json").read_text(encoding="utf-8"))
    assert pending["from"] == "0.2.0" and pending["to"] == "0.9.0" and pending["show"] is False


def test_without_auto_update_only_a_hint_until_the_button(tmp_path, key):
    u, launched, messages = _updater(tmp_path, key, _channel(tmp_path, key), auto=False, idle=lambda: 9999.0)
    u.step(check_due=True)
    assert u.snapshot()["status"] == "available"
    assert not (_updates(tmp_path) / "ZeitspurSetup-0.9.0.exe").exists()   # nichts geladen
    assert launched == [] and any("Jetzt aktualisieren" in m for m in messages)
    u.install_now()
    u.step()
    assert len(launched) == 1 and u.snapshot()["status"] == "installing"
    # auf Knopfdruck: nach dem Neustart kommt das Fenster wieder (im Leerlauf bliebe es im Tray)
    assert json.loads((_updates(tmp_path) / "pending.json").read_text(encoding="utf-8"))["show"] is True


def test_same_or_older_version_is_never_installed(tmp_path, key):
    for version in ("0.2.0", "0.1.5"):
        u, launched, _ = _updater(tmp_path, key, _channel(tmp_path, key, version=version), idle=lambda: 9999.0)
        u.step(check_due=True)
        u.install_now()
        u.step(check_due=True)
        assert u.snapshot()["status"] == "current" and launched == []


def test_foreign_signature_is_never_installed(tmp_path, key):
    url = _channel(tmp_path, Ed25519PrivateKey.generate())   # Angreifer mit eigenem Schluessel
    u, launched, _ = _updater(tmp_path, key, url, idle=lambda: 9999.0)
    u.step(check_due=True)
    assert u.snapshot()["status"] == "error" and launched == []
    assert not _updates(tmp_path).exists() or not list(_updates(tmp_path).glob("*.exe"))


def test_setup_changed_after_download_is_not_started(tmp_path, key):
    idle = [0.0]
    u, launched, _ = _updater(tmp_path, key, _channel(tmp_path, key), idle=lambda: idle[0])
    u.step(check_due=True)                             # geladen, PC noch in Benutzung
    setup = _updates(tmp_path) / "ZeitspurSetup-0.9.0.exe"
    setup.write_bytes(b"manipuliert")
    idle[0] = 9999.0
    u.step()
    assert launched == []                              # das veraenderte Setup startet nie ...
    assert setup.read_bytes() == SETUP and u.snapshot()["status"] == "ready"   # ... es wird neu geladen
    u.step()
    assert len(launched) == 1


def test_launch_failure_is_reported_and_cleaned_up(tmp_path, key):
    def broken(setup, log):
        raise OSError("vom Virenscanner blockiert")

    u, _, _ = _updater(tmp_path, key, _channel(tmp_path, key), idle=lambda: 9999.0, launch=broken)
    u.step(check_due=True)
    state = u.snapshot()
    assert state["status"] == "error" and "Virenscanner" in state["error"]
    assert not (_updates(tmp_path) / "pending.json").exists()


def test_network_hiccup_keeps_a_known_update(tmp_path, key):
    url = _channel(tmp_path, key)
    u, _, _ = _updater(tmp_path, key, url, auto=False)
    u.step(check_due=True)
    (tmp_path / "channel" / "latest.json").unlink()   # Quelle kurz weg
    u.step(check_due=True)
    state = u.snapshot()
    assert state["status"] == "available" and state["version"] == "0.9.0" and state["error"]


def test_result_after_restart(tmp_path, key):
    data = tmp_path / "data"
    folder = data / "updates"
    folder.mkdir(parents=True)
    (folder / "ZeitspurSetup-0.9.0.exe").write_bytes(SETUP)
    (folder / "pending.json").write_text(json.dumps({"from": "0.2.0", "to": "0.9.0"}), encoding="utf-8")
    assert updater.consume_pending(data, current="0.9.0") == ("ok", "0.9.0", False)
    assert not (folder / "pending.json").exists() and not list(folder.glob("*.exe"))   # aufgeraeumt
    assert updater.consume_pending(data, current="0.9.0") is None                     # nur einmal melden


def test_failed_update_is_not_retried_automatically(tmp_path, key):
    data = tmp_path / "data"
    (data / "updates").mkdir(parents=True)
    (data / "updates" / "pending.json").write_text(json.dumps({"from": "0.2.0", "to": "0.9.0"}), encoding="utf-8")
    assert updater.consume_pending(data, current="0.2.0") == ("failed", "0.9.0", False)
    u, launched, _ = _updater(tmp_path, key, _channel(tmp_path, key), idle=lambda: 9999.0)
    u.step(check_due=True)
    u.step()
    assert u.snapshot()["status"] == "ready" and launched == []   # nicht in einer Schleife immer wieder
    u.install_now()                                                 # von Hand geht es
    u.step()
    assert len(launched) == 1


def test_thread_installs_and_stops(tmp_path, key):
    stop = threading.Event()
    launched = []
    u = updater.Updater(Config(), tmp_path / "data", stop, manifest_url=_channel(tmp_path, key),
                        public_key=_pub(key), current="0.2.0", idle_seconds=lambda: 9999.0,
                        launch=lambda setup, log: launched.append(setup), timing=(0.0, 3600.0, 0.0), tick_s=0.02)
    u.start()
    deadline = time.monotonic() + 30
    while not launched and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    u.wake()
    u.join(5)
    assert launched and not u._thread.is_alive()


# ---- Ausgaben ---------------------------------------------------------------------------------------

def test_release_key_is_built_in():
    """Ohne echten oeffentlichen Schluessel koennte kein Client ein Update pruefen."""
    assert len(base64.b64decode(updater.PUBLIC_KEY, validate=True)) == 32


def test_source_and_own_build_never_update_themselves():
    """Nur der Release-Build bringt den Update-Kanal mit (zeitspur.spec) - nie der Quelltext."""
    assert not (Path(edition.__file__).with_name("release_channel.txt")).exists()
    assert edition.UPDATE_CHANNEL is None and edition.features()["updates"] is False


# ---- Leerlaufzeit aus den Einstellungen ------------------------------------------------------------

def test_idle_time_comes_from_the_settings_and_applies_immediately(tmp_path, key):
    cfg = Config()
    cfg.update_idle_minutes = 2
    idle, launched = [119.0], []
    u = updater.Updater(cfg, tmp_path / "data", threading.Event(), manifest_url=_channel(tmp_path, key),
                        public_key=_pub(key), current="0.2.0", idle_seconds=lambda: idle[0],
                        launch=lambda setup, log: launched.append(setup), timing=(0.0, 3600.0, None))
    u.step(check_due=True)
    assert u.snapshot()["idle_s"] == 120.0 and launched == []   # 119 s Leerlauf reichen nicht fuer 2 Minuten
    cfg.update_idle_minutes = 10                                  # zur Laufzeit hochgesetzt (Einstellungen)
    idle[0] = 300.0
    u.step()
    assert launched == [] and u.snapshot()["idle_s"] == 600.0
    idle[0] = 601.0
    u.step()
    assert len(launched) == 1


def test_test_timing_from_environment(monkeypatch):
    monkeypatch.setenv(updater.ENV_TIMING, "20,60")
    assert updater._env_timing() == (20.0, 60.0, None)      # Leerlauf dann aus den Einstellungen
    monkeypatch.setenv(updater.ENV_TIMING, "20,60,30")
    assert updater._env_timing() == (20.0, 60.0, 30.0)
    monkeypatch.setenv(updater.ENV_TIMING, "kaputt")
    assert updater._env_timing() is None


def test_describe_idle():
    assert updater.describe_idle(30) == "30 Sekunden"
    assert updater.describe_idle(60) == "1 Minute"
    assert updater.describe_idle(120) == "2 Minuten"
    assert updater.describe_idle(90) == "2 Minuten"
    assert updater.describe_idle(300) == "5 Minuten"


def test_https_requests_use_the_windows_certificate_check(monkeypatch):
    """Auf frischen PCs fehlen Python sonst Stammzertifikate ('unable to get local issuer certificate')."""
    import urllib.request

    from zeitspur import winutil
    seen = {}

    def fake_urlopen(request, timeout=None, context=None):
        seen["context"] = context
        raise OSError("nur geprueft, nicht verbunden")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(winutil, "https_context", lambda: "WINDOWS-PRUEFUNG")
    with pytest.raises(updater.UpdateError):
        updater.fetch_bytes("https://example.org/latest.json")
    assert seen["context"] == "WINDOWS-PRUEFUNG"


def test_certificate_error_message_is_readable(monkeypatch):
    import ssl
    import urllib.error
    import urllib.request

    def fake_urlopen(request, timeout=None, context=None):
        err = ssl.SSLCertVerificationError(1, "certificate verify failed")
        err.verify_message = "unable to get local issuer certificate"
        raise urllib.error.URLError(err)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(updater.UpdateError, match="Zertifikat der Update-Quelle nicht vertrauenswürdig"):
        updater.fetch_bytes("https://example.org/latest.json")


# ---- Start des Setups und Neustart danach ----------------------------------------------------------

def test_setup_is_started_outside_the_own_process_tree(tmp_path):
    """Das Setup beendet Zeitspur samt Prozessbaum (taskkill /T). Als direktes Kind haette es sich dabei
    selbst mitbeendet - Zeitspur war danach einfach weg (Sandbox-Test 04.10.2026). Deshalb ueber 'start'."""
    setup = tmp_path / "updates" / "ZeitspurSetup-0.9.0.exe"
    line = updater.setup_command(setup, tmp_path / "updates" / "setup.log")
    assert ' /c start "" ' in line and f'"{setup}"' in line
    assert "/SILENT" in line and "/VERYSILENT" not in line      # sichtbares Fortschrittsfenster
    assert "/UPDATE=1" in line and "/SUPPRESSMSGBOXES" in line and f'"/LOG={tmp_path / "updates" / "setup.log"}"' in line


def test_window_comes_back_after_the_update_if_it_was_open(tmp_path, key):
    url = _channel(tmp_path, key)
    for visible in (True, False):
        cfg = Config()
        u = updater.Updater(cfg, tmp_path / f"data{visible}", threading.Event(), manifest_url=url,
                            public_key=_pub(key), current="0.2.0", idle_seconds=lambda: 9999.0,
                            launch=lambda setup, log: None, window_visible=lambda v=visible: v,
                            timing=(0.0, 3600.0, 300.0))
        u.step(check_due=True)
        pending = updater.read_pending(tmp_path / f"data{visible}")
        assert pending["to"] == "0.9.0" and pending["show"] is visible


def test_read_pending_ignores_missing_or_broken_files(tmp_path):
    assert updater.read_pending(tmp_path) is None
    (tmp_path / "updates").mkdir()
    (tmp_path / "updates" / "pending.json").write_text("kaputt", encoding="utf-8")
    assert updater.read_pending(tmp_path) is None


def test_release_seite_nur_aus_gepruefter_version():
    assert updater.release_page("0.4.0") == "https://github.com/Tim-Schaller/Zeitspur/releases/tag/v0.4.0"
    for bad in ("0.4.0/../../boese", "", None, "v0.4"):
        assert updater.release_page(bad) is None


def test_release_seite_oeffnet_nur_die_eigene_adresse(monkeypatch):
    import os

    from zeitspur.app import App
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)

    class Host:
        open_release_page = App.open_release_page

        def __init__(self, status):
            self.status = status

        def update_status(self):
            return self.status

    url = updater.release_page("0.4.0")
    assert Host({"release_url": url}).open_release_page() is True and opened == [url]
    assert Host(None).open_release_page() is False and Host({"release_url": None}).open_release_page() is False
    assert opened == [url]
