"""Fenstersteuerung (Anzeigen/Verstecken) der App.

Hintergrund: pywebview fuehrt den closing-Handler INLINE auf dem GUI-Thread aus
(Event(..., should_lock=True)), und run_js/evaluate_js warten synchron auf eine Rueckmeldung,
die WebView2 ueber genau diesen GUI-Thread zustellt. Ein direkter run_js-Aufruf aus dem
closing-Handler laesst den GUI-Thread also auf sich selbst warten: Das Fenster haengt und
laesst sich nach einmaligem Schliessen nie wieder oeffnen.
"""
import threading
import time

import pytest

from zeitspur.app import App
from zeitspur.config import Config


@pytest.fixture(autouse=True)
def _no_real_window_lookup(monkeypatch):
    """show_window sucht das Fenster ueber seinen Titel - im Test nie das echte Zeitspur-Fenster anfassen."""
    monkeypatch.setattr("zeitspur.app.winutil.ensure_window_on_screen", lambda title: None)


class _JsHost:
    """Minimales Objekt mit der echten _run_js-Implementierung der App."""

    _run_js = App._run_js

    def __init__(self, window, loaded=True):
        self.window = window
        self.window_loaded = threading.Event()
        if loaded:
            self.window_loaded.set()


def test_run_js_never_blocks_the_caller():
    """Der Aufrufer darf nicht warten, auch wenn run_js selbst dauerhaft haengt."""
    started = threading.Event()
    release = threading.Event()

    class FrozenWindow:
        def run_js(self, script):
            started.set()
            release.wait(10)  # simuliert den blockierten GUI-Thread

    host = _JsHost(FrozenWindow())
    t0 = time.monotonic()
    host._run_js("window.zeitspur && window.zeitspur.onHidden()")
    elapsed = time.monotonic() - t0
    try:
        assert elapsed < 1.0, f"_run_js hat den Aufrufer {elapsed:.1f}s blockiert"
        assert started.wait(3), "das Skript wurde gar nicht ausgefuehrt"
    finally:
        release.set()


def test_run_js_skipped_before_window_is_loaded():
    calls = []

    class W:
        def run_js(self, script):
            calls.append(script)

    _JsHost(W(), loaded=False)._run_js("x")
    time.sleep(0.2)
    assert calls == []
    _JsHost(None)._run_js("x")  # ohne Fenster ebenfalls wirkungslos


def test_run_js_swallows_errors():
    class W:
        def run_js(self, script):
            raise RuntimeError("WebView2 weg")

    _JsHost(W())._run_js("x")  # darf nicht nach aussen schlagen
    time.sleep(0.2)


def test_show_window_restores_even_if_show_fails():
    """Ein minimiertes Fenster muss auch dann hochkommen, wenn show() scheitert."""
    calls = []

    class W:
        def show(self):
            calls.append("show")
            raise RuntimeError("kaputt")

        def restore(self):
            calls.append("restore")

    class Host:
        show_window = App.show_window

        def __init__(self):
            self.window = W()
            self._visible = False
            self.js = []

        def _run_js(self, script):
            self.js.append(script)

    host = Host()
    host.show_window()
    assert calls == ["show", "restore"]  # restore wird trotz Fehler in show erreicht
    assert host._visible is True and host.js


def test_show_window_without_window_is_noop():
    class Host:
        show_window = App.show_window
        window = None
        _visible = False

        def _run_js(self, script):
            raise AssertionError("darf nicht aufgerufen werden")

    Host().show_window()


# ---- Hauptfenster erst zeigen, wenn seine Seite geladen ist (Startfenster bis dahin) ------------------------

class _RecordingWindow:
    def __init__(self):
        self.calls = []
        self.js = []

    def show(self):
        self.calls.append("show")

    def restore(self):
        self.calls.append("restore")

    def run_js(self, script):
        self.js.append(script)


class _FakeSplash:
    def __init__(self):
        self.closed = 0

    def close(self, *_args):
        self.closed += 1


def _app(tmp_path):
    app = App(Config(), tmp_path, first_run=True)
    app.window = _RecordingWindow()
    return app


def _wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


def test_window_appears_when_page_is_loaded(tmp_path):
    app, splash = _app(tmp_path), _FakeSplash()
    app.reveal_when_loaded(splash, timeout_s=30)
    assert app.window.calls == [] and splash.closed == 0   # bis dahin steht nur das Startfenster
    app.on_loaded()
    assert app.window.calls == ["show", "restore"]
    assert splash.closed == 1 and app._visible
    assert _wait_for(lambda: app.window.js), "die Seite erfaehrt nicht, dass sie sichtbar ist"
    assert not app._reveal_timer.is_alive()   # Rueckfall-Timer abgebrochen, kein Thread bleibt haengen
    app.on_loaded()   # weitere Ladevorgaenge holen das Fenster nicht erneut hoch (setzte Maximieren zurueck)
    assert app.window.calls == ["show", "restore"] and splash.closed == 1


def test_window_appears_even_if_page_never_loads(tmp_path):
    """Niemand soll vor einem Startfenster sitzen, das nie verschwindet."""
    app, splash = _app(tmp_path), _FakeSplash()
    app.reveal_when_loaded(splash, timeout_s=0.05)
    assert _wait_for(lambda: splash.closed == 1)
    assert app.window.calls == ["show", "restore"] and app._visible
    app.on_loaded()   # laedt die Seite doch noch, wird sie nur benachrichtigt
    assert app.window.calls == ["show", "restore"]
    assert _wait_for(lambda: app.window.js)


def test_no_window_while_quitting_but_splash_closes(tmp_path):
    app, splash = _app(tmp_path), _FakeSplash()
    app.reveal_when_loaded(splash, timeout_s=30)
    app.quitting = True
    app.on_loaded()
    assert app.window.calls == [] and splash.closed == 1


def test_hidden_start_stays_hidden_when_loaded(tmp_path):
    """Autostart: Fenster versteckt, kein Startfenster - Laden darf es nicht hervorholen."""
    app = _app(tmp_path)
    app.on_loaded()
    time.sleep(0.1)
    assert app.window.calls == [] and app.window.js == []


def test_after_idle_update_only_the_splash_shows_then_back_to_tray(tmp_path):
    """Neustart nach einem Update im Leerlauf: Startfenster als Lebenszeichen, das Hauptfenster bleibt zu."""
    app, splash = _app(tmp_path), _FakeSplash()
    app.reveal_when_loaded(splash, timeout_s=30, show_window=False)
    app.on_loaded()
    assert splash.closed == 1 and app.window.calls == [] and not app._visible
