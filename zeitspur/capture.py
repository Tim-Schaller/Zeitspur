"""Screenshot-Loop: Erfassung aller Monitore, Aenderungserkennung, Leerlauf-/Sperr-Erkennung, Deny-Listen.

Ein Frame wird nur gespeichert, wenn er sich gegenueber dem *zuletzt gespeicherten* Frame desselben
Monitors ausreichend unterscheidet, das Vordergrundfenster gewechselt hat oder der laufende Block
zu alt ist. Unveraenderte Frames verlaengern nur ts_end des offenen Eintrags (gepuffert).
Alle Bildverarbeitung passiert im Speicher; es werden keine Bilddateien geschrieben.
"""
from __future__ import annotations

import enum
import io
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from PIL import Image, ImageChops

from . import timeutil, winutil
from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)

DIFF_WIDTH = 160
PIXEL_DELTA = 24
EXTEND_FLUSH_SECONDS = 30
EXTEND_FLUSH_MAX = 50
ERROR_LOG_INTERVAL = 60
WEBP_METHOD = 2              # Kodieraufwand; siehe encode_webp


class CaptureState(str, enum.Enum):
    STARTING = "starting"
    RECORDING = "recording"
    PAUSED = "paused"
    LOCKED = "locked"
    IDLE = "idle"
    EXCLUDED = "excluded"
    NO_DISK = "no_disk"
    ERROR = "error"
    STOPPED = "stopped"


STATE_LABELS = {
    CaptureState.STARTING: "Startet …",
    CaptureState.RECORDING: "Aufnahme läuft",
    CaptureState.PAUSED: "Pausiert",
    CaptureState.LOCKED: "Bildschirm gesperrt",
    CaptureState.IDLE: "Inaktiv (keine Eingaben)",
    CaptureState.EXCLUDED: "Ausgeschlossene App aktiv",
    CaptureState.NO_DISK: "Zu wenig Speicherplatz",
    CaptureState.ERROR: "Fehler bei der Aufnahme",
    CaptureState.STOPPED: "Beendet",
}


# --------------------------------------------------------------------------- Bildverarbeitung

def downscale_for_diff(img: Image.Image, width: int = DIFF_WIDTH) -> Image.Image:
    gray = img.convert("L")
    height = max(1, round(gray.height * width / max(1, gray.width)))
    return gray.resize((width, height), Image.Resampling.BOX)


def frame_difference(a: Image.Image, b: Image.Image, pixel_delta: int = PIXEL_DELTA) -> float:
    """Anteil der Pixel (0..1), deren Helligkeit sich um mehr als pixel_delta unterscheidet."""
    if a.size != b.size or a.mode != b.mode:
        return 1.0
    hist = ImageChops.difference(a, b).histogram()
    total = a.width * a.height
    return sum(hist[pixel_delta + 1:]) / total if total else 1.0


class FrameDiff:
    """Merkt sich je Monitor das Vergleichsbild des zuletzt *gespeicherten* Frames."""

    def __init__(self, threshold: float, width: int = DIFF_WIDTH, pixel_delta: int = PIXEL_DELTA):
        self.threshold = threshold
        self.width = width
        self.pixel_delta = pixel_delta
        self._reference: dict[int, Image.Image] = {}

    def prepare(self, img: Image.Image) -> Image.Image:
        return downscale_for_diff(img, self.width)

    def ratio(self, monitor_id: int, small: Image.Image) -> float:
        ref = self._reference.get(monitor_id)
        if ref is None:
            return 1.0
        return frame_difference(ref, small, self.pixel_delta)

    def is_changed(self, monitor_id: int, small: Image.Image) -> tuple[bool, float]:
        r = self.ratio(monitor_id, small)
        return r > self.threshold, r

    def commit(self, monitor_id: int, small: Image.Image) -> None:
        self._reference[monitor_id] = small

    def reset(self, monitor_id: int | None = None) -> None:
        if monitor_id is None:
            self._reference.clear()
        else:
            self._reference.pop(monitor_id, None)


def encode_webp(img: Image.Image, max_width: int, quality: int) -> tuple[bytes, int, int]:
    if img.width > max_width:
        height = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, height), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    # method=2 statt der Vorgabe 4: gemessen am 21.09.2026 an drei 1920x1200-Bildschirmfotos
    # 73 ms statt 182 ms pro Bild bei 3 % mehr Speicher. Bei drei Monitoren und einer Aufnahme
    # alle 5 s ist das der Unterschied zwischen 0,55 s und 0,22 s Rechenzeit je Zyklus.
    img.save(buf, "WEBP", quality=quality, method=WEBP_METHOD)
    return buf.getvalue(), img.width, img.height


def encode_png_gray(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("L").save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def shot_to_image(shot) -> Image.Image:
    """mss.ScreenShot (BGRA) -> PIL RGB ohne Python-seitige Konvertierung."""
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


# --------------------------------------------------------------------------- Loop

class OcrSink(Protocol):
    def submit(self, entry_id: int, image_bytes: bytes) -> bool: ...


@dataclass
class OpenBlock:
    entry_id: int
    ts_start: int
    ts_end: int
    process_name: str
    window_title: str


class CaptureLoop(threading.Thread):
    """Erfassungs-Thread. Zustand fuer das Tray ueber `state` bzw. on_state-Callback."""

    def __init__(self, cfg: Config, storage: Storage, ocr: OcrSink | None, stop_event: threading.Event,
                 *, on_state: Callable[[CaptureState, str], None] | None = None, start_delay: float = 2.0):
        super().__init__(name="capture", daemon=True)
        self.cfg = cfg
        self.storage = storage
        self.ocr = ocr
        self.stop_event = stop_event
        self.on_state = on_state
        self.start_delay = start_delay
        self.paused = False
        self.state = CaptureState.STARTING
        self.state_detail = ""
        self.frames_seen = 0
        self.entries_created = 0
        self.last_error: str | None = None
        self._diff = FrameDiff(cfg.change_threshold)
        self._blocks: dict[int, OpenBlock] = {}
        self._pending_extend: dict[int, int] = {}
        self._last_flush = time.monotonic()
        self._last_error_log = 0.0
        self._sct = None
        self._process_patterns = cfg.process_patterns()
        self._title_patterns = cfg.title_patterns()
        self._lock = threading.Lock()
        self._reset_requested = threading.Event()

    # ---- Steuerung -------------------------------------------------------
    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            self._set_state(CaptureState.PAUSED)

    def request_reset(self) -> None:
        """Offene Bloecke beim naechsten Tick schliessen (z. B. nachdem Eintraege geloescht wurden).
        Threadsicher: wird nur als Flag gesetzt und im Capture-Thread ausgefuehrt."""
        self._reset_requested.set()

    def refresh_config(self) -> None:
        """Nach Konfigurationsaenderung: Deny-Listen und Schwellwert neu einlesen (Intervall wirkt beim naechsten Tick)."""
        self._process_patterns = self.cfg.process_patterns()
        self._title_patterns = self.cfg.title_patterns()
        self._diff.threshold = self.cfg.change_threshold

    def add_exclusion(self, process_name: str) -> None:
        """Fuegt zur Laufzeit einen Prozess zur Deny-Liste hinzu (Config wird vom Aufrufer gespeichert)."""
        import re

        pattern = re.escape(process_name)
        if pattern not in self.cfg.excluded_process_regex:
            self.cfg.excluded_process_regex.append(pattern)
        self._process_patterns = self.cfg.process_patterns()

    def current_foreground(self) -> winutil.WindowInfo | None:
        return winutil.foreground_window()

    # ---- Thread ----------------------------------------------------------
    def run(self) -> None:
        if self.stop_event.wait(self.start_delay):
            self._finish()
            return
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as e:  # nie den Thread sterben lassen
                self._report_error(f"{type(e).__name__}: {e}")
            interval = max(2, self.cfg.capture_interval_seconds)  # jede Runde lesen: Aenderungen wirken sofort
            elapsed = time.monotonic() - started
            if self.stop_event.wait(max(0.5, interval - elapsed)):
                break
        self._finish()

    def _finish(self) -> None:
        self.flush_extends()
        self._close_blocks()
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
        self._set_state(CaptureState.STOPPED)

    def tick(self) -> None:
        now_ms = timeutil.now_ms()
        if self._reset_requested.is_set():
            self._reset_requested.clear()
            self._close_blocks()
        if self.paused:
            self._suspend(CaptureState.PAUSED)
            return
        if winutil.is_session_locked():
            self._suspend(CaptureState.LOCKED)
            return
        if winutil.idle_seconds() >= self.cfg.idle_pause_minutes * 60:
            self._suspend(CaptureState.IDLE)
            return
        if winutil.free_disk_bytes(self.storage.db_path) < self.cfg.min_free_disk_gb * 1024 ** 3:
            self._suspend(CaptureState.NO_DISK, "Aufnahme pausiert, bis wieder Speicherplatz frei ist")
            return
        fg = self.current_foreground()
        if fg is not None and self.is_excluded(fg):
            self._suspend(CaptureState.EXCLUDED, fg.process_name)
            return
        self._capture_all(fg, now_ms)
        self._maybe_flush()

    # ---- Erfassung -------------------------------------------------------
    def _matches_deny(self, name: str, title: str) -> bool:
        # Laenge deckeln: Fenstertitel setzt jede App beliebig lang; das begrenzt den Worst Case beim Matchen
        # (die Muster sind zusaetzlich beim Laden der Config auf katastrophales Backtracking geprueft).
        name = (name or "")[:256]
        title = (title or "")[:512]
        return any(p.search(name) for p in self._process_patterns) or any(p.search(title) for p in self._title_patterns)

    def is_excluded(self, fg: winutil.WindowInfo) -> bool:
        return self._matches_deny(fg.process_name, fg.title)

    def _screenshots(self) -> list[tuple[int, dict, Image.Image]]:
        import mss
        import mss.exception

        if self._sct is None:
            self._sct = mss.mss()
        try:
            monitors = self._sct.monitors[1:] or self._sct.monitors[:1]
            result = []
            for idx, mon in enumerate(monitors, start=1):
                shot = self._sct.grab(mon)
                result.append((idx, mon, shot_to_image(shot)))
            return result
        except mss.exception.ScreenShotError as e:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
            raise RuntimeError(f"Screenshot fehlgeschlagen: {e}") from e

    def _window_for(self, mon: dict, fg: winutil.WindowInfo | None,
                    top: dict[int, winutil.WindowInfo]) -> winutil.WindowInfo | None:
        """Das fuer diesen Monitor massgebliche Fenster: das Vordergrundfenster, wenn es hier liegt,
        sonst das oberste sichtbare Fenster dieses Monitors."""
        if fg is not None and winutil.point_in_monitor(fg.center, mon):
            return fg
        return top.get(mon["monitor_id"])

    def _capture_all(self, fg: winutil.WindowInfo | None, now_ms: int) -> None:
        frames = self._screenshots()
        self.frames_seen += len(frames)
        monitors = [{**mon, "monitor_id": monitor_id} for monitor_id, mon, _ in frames]
        # Oberstes Fenster je Monitor nur ermitteln, wenn ein Monitor das Vordergrundfenster NICHT enthaelt
        need_top = any(not (fg and winutil.point_in_monitor(fg.center, m)) for m in monitors)
        top = winutil.top_window_per_monitor(monitors) if need_top else {}
        for monitor_id, mon, img in frames:
            win = self._window_for({**mon, "monitor_id": monitor_id}, fg, top)
            process_name = win.process_name if win else ""
            title = win.title if win else ""
            exe_path = win.exe_path if win else None
            # Deny-Liste je Monitor: ein ausgeschlossenes Nicht-Vordergrundfenster wird nur auf SEINEM
            # Monitor uebersprungen (das Vordergrundfenster ist in tick() bereits global geprueft).
            if win is not None and win is not fg and self._matches_deny(process_name, title):
                self._close_monitor_block(monitor_id)
                continue
            small = self._diff.prepare(img)
            changed, ratio = self._diff.is_changed(monitor_id, small)
            block = self._blocks.get(monitor_id)
            window_changed = (block is not None
                              and (block.process_name, block.window_title) != (process_name, title))
            too_old = block is not None and now_ms - block.ts_start >= self.cfg.max_block_ms
            if block is None or changed or window_changed or too_old:
                self._store_frame(monitor_id, img, small, now_ms, process_name, title, exe_path)
                log.debug("Monitor %d: neuer Eintrag (diff=%.3f, fenster=%s, alt=%s)", monitor_id, ratio,
                          window_changed, too_old)
            else:
                block.ts_end = now_ms
                self._pending_extend[block.entry_id] = now_ms
        self._set_state(CaptureState.RECORDING)

    def _close_monitor_block(self, monitor_id: int) -> None:
        """Schliesst den offenen Block eines Monitors (z. B. weil dort jetzt eine ausgeschlossene App liegt)."""
        block = self._blocks.pop(monitor_id, None)
        if block is not None:
            self._pending_extend[block.entry_id] = max(block.ts_end, self._pending_extend.get(block.entry_id, 0))
        self._diff.reset(monitor_id)

    def _store_frame(self, monitor_id: int, img: Image.Image, small: Image.Image, now_ms: int,
                     process_name: str, title: str, exe_path: str | None) -> None:
        old = self._blocks.get(monitor_id)
        if old is not None:
            self._pending_extend[old.entry_id] = max(old.ts_end, self._pending_extend.get(old.entry_id, 0))
        webp, w, h = encode_webp(img, self.cfg.max_image_width, self.cfg.webp_quality)
        entry_id = self.storage.insert_entry(
            ts_start=now_ms, ts_end=now_ms, monitor_id=monitor_id, process_name=process_name,
            window_title=title, exe_path=exe_path, width=w, height=h, webp=webp)
        self.entries_created += 1
        self._diff.commit(monitor_id, small)
        self._blocks[monitor_id] = OpenBlock(entry_id, now_ms, now_ms, process_name, title)
        if self.ocr is not None:
            if not self.ocr.submit(entry_id, encode_png_gray(img)):
                log.debug("OCR-Queue voll, Eintrag %d wird spaeter nachgeholt", entry_id)

    # ---- Puffer / Zustand ------------------------------------------------
    def _maybe_flush(self) -> None:
        if (time.monotonic() - self._last_flush >= EXTEND_FLUSH_SECONDS
                or len(self._pending_extend) >= EXTEND_FLUSH_MAX):
            self.flush_extends()

    def flush_extends(self) -> None:
        if not self._pending_extend:
            self._last_flush = time.monotonic()
            return
        updates, self._pending_extend = self._pending_extend, {}
        try:
            self.storage.extend_entries(updates)
        except Exception as e:
            self._report_error(f"ts_end-Update fehlgeschlagen: {e}")
        self._last_flush = time.monotonic()

    def _close_blocks(self) -> None:
        for block in self._blocks.values():
            self._pending_extend[block.entry_id] = max(block.ts_end, self._pending_extend.get(block.entry_id, 0))
        self._blocks.clear()
        self._diff.reset()
        self.flush_extends()

    def _suspend(self, state: CaptureState, detail: str = "") -> None:
        if self._blocks:
            self._close_blocks()
        self._set_state(state, detail)

    def _set_state(self, state: CaptureState, detail: str = "") -> None:
        with self._lock:
            if state == self.state and detail == self.state_detail:
                return
            self.state, self.state_detail = state, detail
        log.info("Aufnahmezustand: %s%s", state.value, f" ({detail})" if detail and state != CaptureState.EXCLUDED else "")
        if self.on_state:
            try:
                self.on_state(state, detail)
            except Exception:
                log.debug("on_state-Callback fehlgeschlagen", exc_info=True)

    def _report_error(self, message: str) -> None:
        self.last_error = message
        now = time.monotonic()
        if now - self._last_error_log > ERROR_LOG_INTERVAL:
            log.warning("Aufnahmefehler: %s", message)
            self._last_error_log = now
        self._set_state(CaptureState.ERROR, message)
