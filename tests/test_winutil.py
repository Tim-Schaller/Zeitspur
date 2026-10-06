import os
import sys

import pytest

from zeitspur import winutil

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32-API")


def test_idle_and_lock_probes():
    assert winutil.idle_seconds() >= 0.0
    assert isinstance(winutil.is_session_locked(), bool)


def test_foreground_window_shape():
    fg = winutil.foreground_window()
    if fg is not None:
        assert isinstance(fg.title, str) and isinstance(fg.process_name, str) and fg.pid >= 0
        assert len(fg.rect) == 4 and isinstance(fg.center, tuple)


def test_single_instance_mutex():
    name = rf"Local\ZeitspurTest.{os.getpid()}"
    first = winutil.SingleInstance(name)
    assert first.acquire()
    second = winutil.SingleInstance(name)
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_named_events_roundtrip():
    name = rf"Local\ZeitspurTestEvent.{os.getpid()}"
    assert winutil.signal_event(name) is False  # existiert noch nicht
    handle = winutil.create_event(name)
    try:
        assert winutil.wait_for_events([handle], 20) is None
        assert winutil.signal_event(name) is True
        assert winutil.wait_for_events([handle], 500) == 0
        assert winutil.wait_for_events([handle], 20) is None  # Auto-Reset
    finally:
        winutil.close_handle(handle)


def test_point_in_monitor_and_disk():
    mon = {"left": -1920, "top": 0, "width": 1920, "height": 1080}
    assert winutil.point_in_monitor((-100, 500), mon)
    assert not winutil.point_in_monitor((10, 500), mon)
    assert winutil.free_disk_bytes(os.environ["LOCALAPPDATA"]) > 0
    assert winutil.free_disk_bytes(os.path.join(os.environ["LOCALAPPDATA"], "gibt", "es", "nicht")) > 0


def test_monitor_layout_beschreibt_jeden_monitor():
    layout = winutil.monitor_layout()
    assert layout and all(len(r) == 4 and r[2] > r[0] and r[3] > r[1] for r in layout)
    assert winutil.monitor_layout() == layout   # gleich, solange niemand um- oder absteckt


def test_dpi_awareness_call_is_safe():
    assert isinstance(winutil.set_dpi_awareness(), bool)


def test_assign_top_windows_per_monitor():
    from zeitspur.winutil import assign_top_windows
    mon1 = {"monitor_id": 1, "left": 0, "top": 0, "width": 1920, "height": 1080}
    mon2 = {"monitor_id": 2, "left": 1920, "top": 0, "width": 1920, "height": 1080}
    # Z-Reihenfolge oberstes zuerst: Fenster A ganz auf Monitor 1, B ganz auf Monitor 2,
    # C waere auf Monitor 1, kommt aber spaeter -> Monitor 1 ist schon vergeben.
    order = [
        (10, (100, 100, 900, 700)),      # Monitor 1
        (11, (2000, 100, 2800, 700)),    # Monitor 2
        (12, (200, 200, 800, 600)),      # erneut Monitor 1 (ignoriert)
    ]
    res = assign_top_windows(order, [mon1, mon2], resolve=lambda hwnd, rect: hwnd)
    assert res == {1: 10, 2: 11}
    # Fenster ohne Ueberlappung mit irgendeinem Monitor wird ignoriert
    assert assign_top_windows([(20, (5000, 5000, 5100, 5100))], [mon1], resolve=lambda h, r: h) == {}
    # spanning window: groesste Ueberlappung gewinnt den Monitor
    span = [(30, (1800, 0, 3000, 1080))]  # 120px auf M1, 1080px breit auf M2 -> M2
    assert assign_top_windows(span, [mon1, mon2], resolve=lambda h, r: h) == {2: 30}


def test_rect_overlap():
    from zeitspur.winutil import _rect_overlap
    mon = {"left": 0, "top": 0, "width": 100, "height": 100}
    assert _rect_overlap((10, 10, 60, 60), mon) == 2500
    assert _rect_overlap((200, 200, 300, 300), mon) == 0
    assert _rect_overlap((-50, -50, 50, 50), mon) == 2500


def test_ensure_window_on_screen_leaves_hidden_and_missing_windows_alone():
    """Versteckte/minimierte Fenster parkt Windows selbst weit ausserhalb - das darf nicht
    als 'falsche Position' gelten, sonst zerrt man sie staendig herum."""
    from zeitspur import winutil
    assert winutil.ensure_window_on_screen("Dieses Fenster gibt es nicht 12345") is False


def test_resolved_path_und_container_shadow_bei_normalem_ordner(tmp_path):
    """Ein gewoehnlicher Ordner ist nicht umgeleitet - die Sperre darf hier nicht anschlagen."""
    from zeitspur import winutil
    real = winutil.resolved_path(tmp_path)
    assert real is not None
    assert real.lower() == str(tmp_path).lower()
    assert winutil.container_shadow(tmp_path) is None
    assert winutil.resolved_path(tmp_path / "gibt-es-nicht") is None


def test_container_shadow_erkennt_msix_umleitung(tmp_path, monkeypatch):
    """Zeigt der aufgeloeste Pfad in den LocalCache eines App-Pakets, ist es eine Kopie."""
    from zeitspur import winutil
    umgeleitet = (r"C:\Users\x\AppData\Local\Packages\Some.App_abc\LocalCache\Local\Zeitspur")
    monkeypatch.setattr(winutil, "resolved_path", lambda p: umgeleitet)
    assert winutil.container_shadow(tmp_path) == umgeleitet
    # Ein anderer, nur abweichender Pfad (Junction auf ein anderes Laufwerk) ist keine Sandbox
    monkeypatch.setattr(winutil, "resolved_path", lambda p: r"D:\Daten\Zeitspur")
    assert winutil.container_shadow(tmp_path) is None


def test_container_shadow_probe_erkennt_umleitung_vor_dem_ersten_schreiben(tmp_path, monkeypatch):
    """Die Umleitung entsteht erst beim Schreiben: die Probedatei muss sie trotzdem aufdecken."""
    from zeitspur import winutil
    cache = r"C:\Users\x\AppData\Local\Packages\Some.App_abc\LocalCache\Local\Zeitspur"

    def aufgeloest(p):
        return cache + r"\.pfadprobe" if str(p).endswith(".pfadprobe") else str(p)

    monkeypatch.setattr(winutil, "resolved_path", aufgeloest)
    assert winutil.container_shadow(tmp_path) is None            # ohne Probe unauffaellig
    assert winutil.container_shadow(tmp_path, probe=True) == cache
    assert not (tmp_path / ".pfadprobe").exists()                # keine Rueckstaende


def test_container_shadow_probe_laesst_normalen_ordner_in_ruhe(tmp_path):
    from zeitspur import winutil
    assert winutil.container_shadow(tmp_path, probe=True) is None
    assert not (tmp_path / ".pfadprobe").exists()


def test_set_thread_low_priority_laeuft_durch():
    """Nur Prioritaet senken - der Hintergrundmodus wuerde OCR auf Sparkerne zwingen."""
    from zeitspur import winutil
    winutil.set_thread_low_priority()   # darf nicht werfen


def _fake_registry(monkeypatch, werte):
    """werte: {(hive, pfad-endung): pv} - alles andere gilt als nicht vorhanden."""
    import winreg

    class Key:
        def __init__(self, pv):
            self.pv = pv

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_key(hive, pfad):
        for (h, endung), pv in werte.items():
            if h == hive and pfad.endswith(endung):
                return Key(pv)
        raise OSError("nicht vorhanden")

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: (key.pv, winreg.REG_SZ))


def test_webview2_erkennung_wie_pywebview(monkeypatch):
    """Ohne WebView2 wiche pywebview still auf den IE-Renderer aus - die Erkennung muss also stimmen."""
    import winreg

    from zeitspur import winutil
    runtime = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    _fake_registry(monkeypatch, {})
    assert winutil.webview2_version() is None                                  # Windows Sandbox
    _fake_registry(monkeypatch, {(winreg.HKEY_CURRENT_USER, runtime): "120.0.2210.91"})
    assert winutil.webview2_version() == "120.0.2210.91"                       # pro Benutzer installiert
    _fake_registry(monkeypatch, {(winreg.HKEY_LOCAL_MACHINE, runtime): "0.0.0.0"})
    assert winutil.webview2_version() is None                                  # Microsoft: 0.0.0.0 = fehlt
    _fake_registry(monkeypatch, {(winreg.HKEY_LOCAL_MACHINE, runtime): "80.0.361.111"})
    assert winutil.webview2_version() is None                                  # zu alt fuer pywebview
    canary = "{65C35B14-6C1D-4122-AC46-7148CC9D6497}"
    _fake_registry(monkeypatch, {(winreg.HKEY_CURRENT_USER, canary): "142.0.1.0"})
    assert winutil.webview2_version() == "142.0.1.0"                           # Vorschaukanal zaehlt auch


def test_webview2_download_knopf():
    from zeitspur import winutil
    from zeitspur.service_main import IDYES, offer_webview2_download
    geoeffnet = []
    assert offer_webview2_download(lambda text, flags: IDYES, geoeffnet.append) is True
    assert geoeffnet == [winutil.WEBVIEW2_DOWNLOAD_URL]
    assert offer_webview2_download(lambda text, flags: 7, geoeffnet.append) is False   # Nein
    assert geoeffnet == [winutil.WEBVIEW2_DOWNLOAD_URL]                                # nichts Neues geoeffnet


def test_dunkelmodus_wie_windows(monkeypatch):
    """Danach richten sich Fensterhintergrund und Startfenster - falsch erkannt, blitzte es beim Start hell auf."""
    import winreg

    from zeitspur import winutil
    personalize = r"Themes\Personalize"
    _fake_registry(monkeypatch, {(winreg.HKEY_CURRENT_USER, personalize): 0})
    assert winutil.apps_use_dark_theme() is True
    _fake_registry(monkeypatch, {(winreg.HKEY_CURRENT_USER, personalize): 1})
    assert winutil.apps_use_dark_theme() is False
    _fake_registry(monkeypatch, {})
    assert winutil.apps_use_dark_theme() is False   # Wert fehlt: Windows nimmt hell an


def test_https_context_prueft_wie_windows():
    """Zertifikatspruefung nie abgeschaltet - und ueber Windows, damit sie auch auf frischen PCs klappt."""
    import ssl

    from zeitspur import winutil
    ctx = winutil.https_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    import truststore
    assert isinstance(ctx, truststore.SSLContext)
