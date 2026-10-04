"""Startfenster: sofort sichtbar, solange Zeitspur beim Start noch nichts anderes zeigen kann.

Beim ersten Start - und nach jedem Update - vergehen mehrere Sekunden, bis das Hauptfenster erscheint: Python,
.NET und WebView2 starten kalt, und der Virenscanner prueft dabei tausende Dateien. Ohne Rueckmeldung sieht das
aus, als sei nichts passiert (beobachtet im Sandbox-Test am 03.10.2026). Das Startfenster erscheint, bevor die
schweren Bibliotheken geladen werden, zeigt einen laufenden Balken und schliesst sich, sobald das Hauptfenster
geladen ist. Nur bei sichtbarem Start (--show) - nicht beim Autostart und nicht im MCP-Modus.

Bewusst selbst gezeichnet statt mit dem Windows-Fortschrittsbalken: dessen Laufanimation braucht die
Common Controls v6, die vor dem Laden der Oberflaeche nicht sicher aktiv sind. Eigener Thread mit eigener
Nachrichtenschleife, damit die Animation weiterlaeuft, waehrend der Hauptthread laedt.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CLASS_NAME = "ZeitspurSplash"
WIDTH, HEIGHT = 420, 132          # bei 96 dpi
TIMER_ID, TIMER_MS = 1, 30
# Farben wie im Zeitstrahl (style.css: --panel, --text, --muted, --accent; Spur etwas kraeftiger als --panel-2),
# hell bzw. dunkel nach der Windows-Einstellung - so wechselt beim Uebergang zum Hauptfenster nicht die Helligkeit.
PALETTES = {
    False: {"background": (0xFF, 0xFF, 0xFF), "text": (0x1C, 0x20, 0x27), "muted": (0x66, 0x70, 0x85),
            "track": (0xE4, 0xE7, 0xEC), "accent": (0x2F, 0x6F, 0xED)},
    True: {"background": (0x1D, 0x21, 0x28), "text": (0xE6, 0xE8, 0xEE), "muted": (0x98, 0xA2, 0xB3),
           "track": (0x34, 0x3A, 0x46), "accent": (0x5B, 0x8D, 0xEF)},
}


def _rgb(c: tuple[int, int, int]) -> int:
    return c[0] | (c[1] << 8) | (c[2] << 16)


class Splash:
    """Kleines Fenster "Zeitspur wird gestartet ..." mit laufendem Balken. Thread-sicher bedienbar.

    Ohne show() sind alle Methoden wirkungslos - der Aufrufer muss nicht unterscheiden, ob es angezeigt wird.
    """

    def __init__(self, text: str = "Wird gestartet …", *, hint: str = "Beim ersten Start kann das einen Moment dauern.",
                 activate: bool = True, dark: bool | None = None):
        self._text = text
        self._hint = hint
        self._activate = activate      # False: erscheint, ohne den Fokus zu nehmen (Tests)
        self._dark = dark              # None: wie Windows (Standard-App-Modus)
        self._colors = PALETTES[False]
        self._hwnd = 0
        self._offset = 0
        self._scale = 1.0
        self._fonts: tuple = ()
        self._icon = 0
        self._class_name = f"{CLASS_NAME}.{id(self):x}"   # je Fenster eigene Klasse mit eigener Fensterprozedur
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="splash", daemon=True)

    # ---- von aussen ------------------------------------------------------------------------
    def show(self) -> "Splash":
        """Zeigt das Fenster - hoechstens einmal; nach close() bleibt es zu."""
        if IS_WINDOWS and self._thread.ident is None:
            self._thread.start()
            self._ready.wait(3.0)
        return self

    @property
    def visible(self) -> bool:
        return bool(self._hwnd)

    def set_text(self, text: str) -> None:
        self._text = text
        if self._hwnd:
            import win32gui

            try:
                win32gui.InvalidateRect(self._hwnd, None, False)
            except Exception:
                pass

    def close(self, *_args) -> None:
        """Schliesst das Fenster (mehrfach aufrufbar; auch als pywebview-Ereignis-Handler)."""
        hwnd, self._hwnd = self._hwnd, 0
        if hwnd:
            import win32con
            import win32gui

            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(3.0)

    # ---- Fensterthread ---------------------------------------------------------------------
    def _run(self) -> None:
        import win32api
        import win32gui

        hinst = win32api.GetModuleHandle(None)
        try:
            self._create(hinst)
        except Exception:
            log.debug("Startfenster liess sich nicht anzeigen", exc_info=True)
            self._hwnd = 0
            self._ready.set()
            self._unregister(hinst)
            return
        self._ready.set()
        win32gui.PumpMessages()
        self._unregister(hinst)

    def _unregister(self, hinst) -> None:
        import win32gui

        try:
            win32gui.UnregisterClass(self._class_name, hinst)
            if self._icon:
                win32gui.DestroyIcon(self._icon)
        except Exception:
            pass
        self._icon = 0

    def _create(self, hinst) -> None:
        import win32api
        import win32con
        import win32gui

        self._scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
        if self._dark is None:
            from . import winutil

            self._dark = winutil.apps_use_dark_theme()
        self._colors = PALETTES[self._dark]
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = self._class_name
        wc.hInstance = hinst
        wc.lpfnWndProc = self._wndproc
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_APPSTARTING)   # Mauszeiger "arbeitet"
        try:
            icon = win32gui.ExtractIcon(0, sys.executable, 0)   # Programmsymbol fuer die Taskleiste
            if icon > 1:   # 0 = kein Symbol, 1 = keine Programmdatei
                self._icon = wc.hIcon = icon
        except Exception:
            pass
        win32gui.RegisterClass(wc)
        w, h = int(WIDTH * self._scale), int(HEIGHT * self._scale)
        work = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY))["Work"]
        x = work[0] + (work[2] - work[0] - w) // 2
        y = work[1] + (work[3] - work[1] - h) // 2
        self._fonts = (self._font(17, 600), self._font(10, 400), self._font(9, 400))
        self._hwnd = win32gui.CreateWindowEx(
            0, self._class_name, "Zeitspur", win32con.WS_POPUP | win32con.WS_BORDER,
            x, y, w, h, 0, 0, hinst, None)
        ctypes.windll.user32.SetTimer(self._hwnd, TIMER_ID, TIMER_MS, None)
        win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNORMAL if self._activate else win32con.SW_SHOWNOACTIVATE)
        win32gui.UpdateWindow(self._hwnd)
        if self._activate:
            try:
                win32gui.SetForegroundWindow(self._hwnd)
            except Exception:
                pass   # Windows erlaubt das nicht immer - sichtbar ist das Fenster trotzdem

    def _font(self, points: int, weight: int):
        import win32gui

        lf = win32gui.LOGFONT()
        lf.lfFaceName = "Segoe UI"
        lf.lfHeight = -int(points * self._scale * 96 / 72)
        lf.lfWeight = weight
        lf.lfQuality = 5   # CLEARTYPE_QUALITY
        return win32gui.CreateFontIndirect(lf)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        import win32con
        import win32gui

        if msg == win32con.WM_PAINT:
            self._paint(hwnd)
            return 0
        if msg == win32con.WM_ERASEBKGND:
            return 1   # alles wird in WM_PAINT gemalt - kein Flackern
        if msg == win32con.WM_TIMER:
            self._offset = (self._offset + max(1, int(4 * self._scale))) % 100000
            win32gui.InvalidateRect(hwnd, None, False)
            return 0
        if msg == win32con.WM_CLOSE:
            ctypes.windll.user32.KillTimer(hwnd, TIMER_ID)
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == win32con.WM_DESTROY:
            for f in self._fonts:
                win32gui.DeleteObject(f)
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _paint(self, hwnd) -> None:
        import win32con
        import win32gui

        hdc, ps = win32gui.BeginPaint(hwnd)
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        w, h = right - left, bottom - top
        # Doppelt gepuffert: erst in ein Bild im Speicher, dann in einem Rutsch aufs Fenster
        mem = win32gui.CreateCompatibleDC(hdc)
        bmp = win32gui.CreateCompatibleBitmap(hdc, w, h)
        old_bmp = win32gui.SelectObject(mem, bmp)
        try:
            s, c = self._scale, self._colors
            pad = int(22 * s)
            bg = win32gui.CreateSolidBrush(_rgb(c["background"]))
            win32gui.FillRect(mem, (0, 0, w, h), bg)
            win32gui.DeleteObject(bg)
            win32gui.SetBkMode(mem, win32con.TRANSPARENT)

            title_font, text_font, hint_font = self._fonts
            win32gui.SelectObject(mem, title_font)
            win32gui.SetTextColor(mem, _rgb(c["text"]))
            win32gui.DrawText(mem, "Zeitspur", -1, (pad, int(16 * s), w - pad, int(46 * s)),
                              win32con.DT_LEFT | win32con.DT_SINGLELINE | win32con.DT_VCENTER)
            win32gui.SelectObject(mem, text_font)
            win32gui.DrawText(mem, self._text, -1, (pad, int(50 * s), w - pad, int(72 * s)),
                              win32con.DT_LEFT | win32con.DT_SINGLELINE | win32con.DT_VCENTER | win32con.DT_END_ELLIPSIS)

            # Laufbalken: ein Block wandert durch die Spur und taucht links wieder auf
            track = (pad, int(84 * s), w - pad, int(90 * s))
            tb = win32gui.CreateSolidBrush(_rgb(c["track"]))
            win32gui.FillRect(mem, track, tb)
            win32gui.DeleteObject(tb)
            span = track[2] - track[0]
            block = max(1, span // 3)
            start = track[0] + (self._offset % (span + block)) - block
            ab = win32gui.CreateSolidBrush(_rgb(c["accent"]))
            win32gui.FillRect(mem, (max(track[0], start), track[1], min(track[2], start + block), track[3]), ab)
            win32gui.DeleteObject(ab)

            win32gui.SelectObject(mem, hint_font)
            win32gui.SetTextColor(mem, _rgb(c["muted"]))
            win32gui.DrawText(mem, self._hint, -1,
                              (pad, int(98 * s), w - pad, int(118 * s)),
                              win32con.DT_LEFT | win32con.DT_SINGLELINE | win32con.DT_VCENTER)
            win32gui.BitBlt(hdc, 0, 0, w, h, mem, 0, 0, win32con.SRCCOPY)
        finally:
            win32gui.SelectObject(mem, old_bmp)
            win32gui.DeleteObject(bmp)
            win32gui.DeleteDC(mem)
            win32gui.EndPaint(hwnd, ps)
