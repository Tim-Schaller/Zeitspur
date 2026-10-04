"""Startfenster: erscheint, laesst sich schliessen und hinterlaesst weder Fenster noch Thread."""
import sys
import threading

import pytest

from zeitspur.splash import Splash


def test_methods_are_harmless_without_show():
    """Der Aufrufer muss nicht unterscheiden, ob das Startfenster wirklich angezeigt wurde."""
    s = Splash()
    s.set_text("Programmteile werden geladen …")
    s.close()
    s.close()
    assert s.visible is False


@pytest.mark.skipif(sys.platform != "win32", reason="Win32-Fenster")
def test_lifecycle_window_and_thread_are_gone_after_close():
    import win32gui

    s = Splash(activate=False).show()   # ohne Fokuswechsel - der Testlauf soll niemandem die Eingabe stehlen
    try:
        assert s.visible
        hwnd = s._hwnd
        assert win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
        s.set_text("Oberfläche wird aufgebaut …")
    finally:
        s.close()
    assert s.visible is False
    assert not win32gui.IsWindow(hwnd)
    assert not s._thread.is_alive()
    assert not [t for t in threading.enumerate() if t.name == "splash"]

    s.show()   # nach close() bleibt es zu - kein zweiter Thread, keine Ausnahme
    assert s.visible is False


@pytest.mark.skipif(sys.platform != "win32", reason="Win32-Fenster")
def test_second_splash_in_same_process_works():
    """Jedes Startfenster registriert seine eigene Fensterklasse und gibt sie wieder frei."""
    for _ in range(2):
        s = Splash(activate=False).show()
        try:
            assert s.visible
        finally:
            s.close()
        assert not s._thread.is_alive()


def _css_colors():
    """Farbvariablen aus style.css: {False: hell, True: dunkel}."""
    import re
    from pathlib import Path

    import zeitspur

    css = (Path(zeitspur.__file__).parent / "timeline_ui" / "style.css").read_text(encoding="utf-8")
    light_part, dark_part = css.split("@media (prefers-color-scheme: dark)", 1)

    def parse(block):
        return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", block))

    light = parse(light_part)
    return {False: light, True: {**light, **parse(dark_part.split("}", 1)[0])}}


def test_colors_match_the_timeline():
    """Startfenster und Fensterhintergrund in den Farben des Zeitstrahls - sonst wechselt beim Uebergang
    die Helligkeit (oder es blitzt im Dunkelmodus weiss auf)."""
    from zeitspur.splash import PALETTES
    from zeitspur.timeline_ui.bridge import PAGE_BACKGROUND

    css = _css_colors()
    for dark in (False, True):
        palette = {name: "#%02x%02x%02x" % rgb for name, rgb in PALETTES[dark].items()}
        assert palette["background"] == css[dark]["--panel"]
        assert palette["text"] == css[dark]["--text"]
        assert palette["muted"] == css[dark]["--muted"]
        assert palette["accent"] == css[dark]["--accent"]
        assert PAGE_BACKGROUND[dark] == css[dark]["--bg"]
