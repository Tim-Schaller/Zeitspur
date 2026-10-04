"""Tray-Icon (pystray) mit Zustandsfarbe und Kontextmenue.

pystray laeuft unter Windows problemlos in einem Nebenthread; der MainThread gehoert pywebview.
Alle Menue-Callbacks delegieren an die App, die selbst fuer Thread-Sicherheit sorgt.
"""
from __future__ import annotations

import logging
import math
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

from . import autostart, edition
from .capture import STATE_LABELS, CaptureState

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

log = logging.getLogger(__name__)

GREY = (140, 140, 140)
STATE_COLORS: dict[CaptureState, tuple[int, int, int]] = {
    CaptureState.RECORDING: (52, 168, 83),
    CaptureState.PAUSED: (244, 180, 0),
    CaptureState.EXCLUDED: (66, 133, 244),
    CaptureState.NO_DISK: (219, 68, 55),
    CaptureState.ERROR: (219, 68, 55),
}


BADGE_TOP = (58, 66, 86)       # Verlauf oben (heller)
BADGE_BOTTOM = (21, 25, 34)    # Verlauf unten (dunkler)
SUPERSAMPLE = 4                # 4-fach zeichnen und verkleinern -> glatte Kanten auch bei 16 px
ANIMATION_FRAMES = 12          # Einzelbilder einer Ringumdrehung
ANIMATION_INTERVAL_S = 0.18    # ~6 Bilder/s aus dem Cache - kostet keine messbare Rechenzeit


def _vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    strip = Image.new("RGB", (1, size))
    px = strip.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return strip.resize((size, size), Image.BILINEAR)


def make_icon_image(color: tuple[int, int, int] = GREY, size: int = 64, *,
                    phase: float | None = None) -> Image.Image:
    """Badge mit Zeit-Ring und Aufnahmepunkt.

    `phase` (0..1) dreht das helle Ringsegment und laesst den Punkt "atmen" -> animiertes Tray-Symbol;
    None zeichnet die statische Variante (Programmsymbol, Installer).
    """
    ss = SUPERSAMPLE
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    overlay = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    # Unter ~28 px (Tray) traegt kein Rahmen mehr: dort fuellt die Marke die Flaeche, sonst wird sie
    # zusammen mit dem dunklen Badge zu einem unlesbaren Fleck. Ab 32 px das vollflaechige Programmsymbol.
    compact = size < 28
    if compact:
        inset, width, dot = s * 0.055, s * 0.150, 0.175
    else:
        pad = s * 0.055
        radius = s * 0.235
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle([pad, pad, s - pad, s - pad], radius=radius, fill=255)
        img.paste(_vertical_gradient(s, BADGE_TOP, BADGE_BOTTOM), (0, 0), mask)
        d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=radius,
                            outline=(255, 255, 255, 46), width=max(1, round(s * 0.011)))  # Lichtkante = Tiefe
        inset, width, dot = s * 0.205, s * 0.092, 0.140

    box = [inset, inset, s - inset, s - inset]
    width = max(2, round(width))
    d.arc(box, 0, 360, fill=color + (85,), width=width)          # gedaempfter Grundring (die "Zeitachse")
    if phase is None:
        d.arc(box, -96, 168, fill=color + (255,), width=width)   # statisch: offener Bogen
    else:
        start = phase * 360.0 - 90.0
        d.arc(box, start, start + 104, fill=color + (255,), width=width)  # laufendes Segment

    # Aufnahmepunkt (atmet leicht mit der Animation)
    breathe = 1.0 if phase is None else 1.0 + 0.10 * math.sin(phase * 2 * math.pi)
    r = s * dot * breathe
    c = s / 2.0
    d.ellipse([c - r, c - r, c + r, c + r], fill=color + (255,))

    return Image.alpha_composite(img, overlay).resize((size, size), Image.LANCZOS)


def animation_frames(color: tuple[int, int, int], size: int = 64, frames: int = ANIMATION_FRAMES) -> list[Image.Image]:
    """Einmalig vorgerenderte Einzelbilder einer Umdrehung (das Tray blendet sie nur noch um)."""
    return [make_icon_image(color, size, phase=i / frames) for i in range(frames)]


def save_ico(path: Path, color: tuple[int, int, int] = (52, 168, 83)) -> Path:
    """Erzeugt eine Multi-Size-ICO-Datei (fuer EXE/Installer).

    Jede Groesse wird einzeln gezeichnet statt aus 256 px heruntergerechnet - sonst verschwimmen
    Ring und Punkt bei 16/24 px zu einem Fleck.
    """
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [make_icon_image(color, n) for n in sizes]
    path.parent.mkdir(parents=True, exist_ok=True)
    images[-1].save(path, format="ICO", sizes=[(n, n) for n in sizes],
                    append_images=images[:-1])
    return path


def tray_icon_size(default: int = 16) -> int:
    """Groesse, in der Windows das Tray-Symbol darstellt (SM_CXSMICON, haengt an der Skalierung).

    Wichtig: Wird das Bild groesser geliefert, skaliert Windows es herunter und das Ringmotiv
    verschwimmt. Deshalb zeichnen wir gleich in der Zielgroesse.
    """
    try:
        import ctypes

        value = int(ctypes.windll.user32.GetSystemMetrics(49))
    except Exception:
        return default
    return value if 12 <= value <= 64 else default


class TrayIcon:
    def __init__(self, app: "App"):
        self.app = app
        self._size = tray_icon_size()
        self._images: dict[tuple[int, int, int], Image.Image] = {}
        self._frames: dict[tuple[int, int, int], list[Image.Image]] = {}
        self._color = GREY
        self._animate = False
        self._anim_stop = threading.Event()
        self._anim_wake = threading.Event()
        self._anim_thread: threading.Thread | None = None
        self.ready = threading.Event()
        self._icon = pystray.Icon("Zeitspur", self._image(GREY), "Zeitspur – startet …", menu=Menu(*self._items()))

    # ---- Aufbau ----------------------------------------------------------
    def _image(self, color: tuple[int, int, int]) -> Image.Image:
        if color not in self._images:
            self._images[color] = make_icon_image(color, self._size)
        return self._images[color]

    def _frames_for(self, color: tuple[int, int, int]) -> list[Image.Image]:
        if color not in self._frames:
            self._frames[color] = animation_frames(color, self._size)
        return self._frames[color]

    def _items(self) -> list:
        app = self.app
        return [
            MenuItem("Zeitstrahl öffnen", lambda: app.show_window(), default=True),
            MenuItem("Aufnahme pausieren", lambda: app.toggle_paused(), checked=lambda item: app.is_paused()),
            MenuItem("Aktuelle App ausschließen", self._exclude_current),
            MenuItem("Letzte 15 Minuten löschen", self._delete_recent),
            Menu.SEPARATOR,
            MenuItem(lambda item: self._status_line(), None, enabled=False),
            MenuItem(lambda item: self._stats_line(), None, enabled=False),
            MenuItem("Mit Windows starten", self._toggle_autostart,
                     checked=lambda item: autostart.is_enabled()),
            MenuItem("Log-Ordner öffnen", lambda: app.open_logs()),
            MenuItem("Datenbank komprimieren", self._compact),
            MenuItem("Nach Updates suchen", lambda: app.check_updates(), visible=bool(edition.UPDATE_CHANNEL)),
            Menu.SEPARATOR,
            MenuItem("Beenden", lambda: app.request_quit()),
        ]

    # ---- Laufzeit --------------------------------------------------------
    def run(self) -> None:
        """Blockiert bis stop(); im eigenen Thread aufrufen."""
        try:
            self._icon.run(setup=self._setup)
        except Exception:
            log.exception("Tray-Icon beendet sich mit Fehler")

    def _setup(self, icon) -> None:
        icon.visible = True
        self._anim_thread = threading.Thread(target=self._animate_loop, name="tray-anim", daemon=True)
        self._anim_thread.start()
        self.ready.set()

    def stop(self) -> None:
        self._anim_stop.set()
        self._anim_wake.set()
        thread = self._anim_thread
        if thread is not None and thread.is_alive():
            thread.join(1.0)
        try:
            self._icon.stop()
        except Exception:
            log.debug("Tray stop fehlgeschlagen", exc_info=True)

    def _animate_loop(self) -> None:
        """Laesst den Ring waehrend der Aufnahme umlaufen; in jedem anderen Zustand steht das Symbol still."""
        index = 0
        while not self._anim_stop.is_set():
            if not self._animate:
                self._anim_wake.wait(1.0)
                self._anim_wake.clear()
                continue
            frames = self._frames_for(self._color)
            index = (index + 1) % len(frames)
            try:
                self._icon.icon = frames[index]
            except Exception:
                log.debug("Tray-Animation beendet", exc_info=True)
                return
            self._anim_stop.wait(ANIMATION_INTERVAL_S)

    def update_state(self, state: CaptureState, detail: str = "") -> None:
        label = STATE_LABELS.get(state, state.value)
        if detail and state == CaptureState.EXCLUDED:
            label = f"{label}: {detail}"
        color = STATE_COLORS.get(state, GREY)
        self._color = color
        self._animate = state == CaptureState.RECORDING
        try:
            if not self._animate:
                self._icon.icon = self._image(color)  # ausserhalb der Aufnahme bewusst ein ruhiges Standbild
            self._icon.title = f"Zeitspur – {label}"
        except Exception:
            log.debug("Tray-Update fehlgeschlagen", exc_info=True)
        if self._animate:
            self._anim_wake.set()

    def notify(self, message: str, title: str = "Zeitspur") -> None:
        if not getattr(pystray.Icon, "HAS_NOTIFICATION", False):
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            log.debug("Tray-Benachrichtigung fehlgeschlagen", exc_info=True)

    # ---- Menue-Texte -----------------------------------------------------
    def _status_line(self) -> str:
        try:
            st = self.app.status_summary()
        except Exception:
            return "Status: unbekannt"
        text = f"Status: {st['label']}"
        if st.get("detail") and st["state"] == CaptureState.EXCLUDED.value:
            text += f" ({st['detail']})"
        return text

    def _stats_line(self) -> str:
        try:
            st = self.app.status_summary()
        except Exception:
            return "Keine Statistik verfügbar"
        if not st.get("ready"):
            return "Datenbank noch nicht geöffnet"
        return (f"Heute {st['entries_today']} Einträge · DB {st['db_size_mb']:.0f} MB"
                f" · OCR offen {st['pending_ocr']}")

    # ---- Aktionen --------------------------------------------------------
    def _exclude_current(self, icon=None, item=None) -> None:
        name = self.app.exclude_current_app()
        if name:
            self.notify(f"„{name}“ wird ab jetzt nicht mehr aufgenommen.")
        else:
            self.notify("Kein Vordergrundfenster erkannt.")

    def _delete_recent(self, icon=None, item=None) -> None:
        deleted = self.app.delete_recent(15)
        self.notify(f"{deleted} Einträge der letzten 15 Minuten gelöscht.")

    def _compact(self, icon=None, item=None) -> None:
        self.notify(self.app.compact_database())

    def _toggle_autostart(self, icon=None, item=None) -> None:
        enabled = autostart.set_enabled(not autostart.is_enabled())
        self.notify("Zeitspur startet ab jetzt automatisch mit Windows."
                    if enabled else "Automatischer Start mit Windows ist ausgeschaltet.")
