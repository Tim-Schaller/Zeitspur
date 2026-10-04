"""Autostart-Eintrag (HKCU\\...\\Run) und Icon-Erzeugung.

Die Registry-Tests arbeiten ausschliesslich unter einem eigenen Testschluessel - der echte
Run-Schluessel des Benutzers darf nie angefasst werden.
"""
import sys

import pytest

from zeitspur import autostart

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Registry/Icons nur unter Windows")

TEST_KEY = r"Software\ZeitspurTest\AutostartCase"


@pytest.fixture
def isolated_run_key(monkeypatch):
    """Lenkt das Modul auf einen Wegwerf-Schluessel um und raeumt ihn danach weg."""
    import winreg

    monkeypatch.setattr(autostart, "RUN_KEY", TEST_KEY)
    yield TEST_KEY
    for key in (TEST_KEY, TEST_KEY.rsplit("\\", 1)[0]):  # Unterschluessel zuerst, dann der leere Elternteil
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


def test_enable_disable_roundtrip(isolated_run_key):
    assert autostart.is_enabled() is False
    assert autostart.enable() is True
    assert autostart.is_enabled() is True
    cmd = autostart.current_command()
    assert cmd and "--autostart" in cmd
    assert autostart.disable() is True
    assert autostart.is_enabled() is False


def test_disable_is_idempotent(isolated_run_key):
    assert autostart.disable() is True  # nicht gesetzt -> trotzdem Erfolg
    assert autostart.is_enabled() is False


def test_set_enabled_returns_effective_state(isolated_run_key):
    assert autostart.set_enabled(True) is True
    assert autostart.set_enabled(False) is False


def test_enable_overwrites_stale_path(isolated_run_key):
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TEST_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, autostart.VALUE_NAME, 0, winreg.REG_SZ, r'"C:\Alt\Weg\Zeitspur.exe" --autostart')
    assert autostart.is_enabled() is True
    autostart.enable()
    assert autostart.current_command() == autostart.target_command()


def test_target_command_contains_autostart_flag():
    cmd = autostart.target_command()
    assert cmd.endswith("--autostart") and cmd.startswith('"')


# --------------------------------------------------------------------------- Symbol

def test_icon_sizes_and_modes():
    from zeitspur.tray import ANIMATION_FRAMES, animation_frames, make_icon_image

    for n in (16, 20, 24, 32, 64, 256):
        img = make_icon_image((52, 168, 83), n)
        assert img.size == (n, n) and img.mode == "RGBA"
    # Tray-Groessen zeichnen ohne Badge (Marke fuellt die Flaeche), grosse Groessen mit Badge:
    # in der Ecke ist das kompakte Symbol transparent, das Badge dagegen deckend.
    assert make_icon_image((52, 168, 83), 16).getpixel((0, 0))[3] == 0
    assert make_icon_image((52, 168, 83), 64).getpixel((32, 4))[3] > 0
    frames = animation_frames((52, 168, 83), 20)
    assert len(frames) == ANIMATION_FRAMES
    assert frames[0].tobytes() != frames[3].tobytes()  # der Ring laeuft wirklich um


def test_save_ico_is_multi_size(tmp_path):
    from PIL import Image

    from zeitspur.tray import save_ico

    out = save_ico(tmp_path / "icon.ico")
    assert out.exists() and out.stat().st_size > 0
    with Image.open(out) as im:
        assert (16, 16) in im.ico.sizes() and (256, 256) in im.ico.sizes()
