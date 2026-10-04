"""Asynchrone OCR-Verarbeitung in einem Hintergrund-Thread mit niedriger Prioritaet.

Die Queue haelt (entry_id, PNG-Bytes) im Speicher. Ist sie leer, werden offene Eintraege
(ocr_status = 0) aus der Datenbank nachgeholt - dafuer wird das gespeicherte WebP entschluesselt
und im Speicher dekodiert. Dadurch gehen nach einem Absturz keine OCR-Jobs verloren.
"""
from __future__ import annotations

import io
import logging
import queue
import threading
import time
from typing import Callable

from PIL import Image

from . import winutil
from .ocr import OcrError, OcrResult, TesseractEngine
from .storage import OCR_DONE, Storage

log = logging.getLogger(__name__)

BACKLOG_INTERVAL = 5.0       # Sekunden zwischen zwei Nachhol-Versuchen bei leerer Queue
BACKLOG_MIN_AGE_MS = 20_000  # frische Eintraege stecken evtl. noch in der Queue eines anderen Pfads


class OcrWorker(threading.Thread):
    def __init__(self, engine: TesseractEngine, storage: Storage, stop_event: threading.Event, *,
                 maxsize: int = 20, on_result: Callable[[int, OcrResult], None] | None = None,
                 backlog_enabled: bool = True):
        super().__init__(name="ocr", daemon=True)
        self.engine = engine
        self.storage = storage
        self.stop_event = stop_event
        self.on_result = on_result
        self.backlog_enabled = backlog_enabled
        self._queue: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=maxsize)
        self._inflight: set[int] = set()
        self._inflight_lock = threading.Lock()
        self._last_backlog = 0.0
        self.processed = 0
        self.failed = 0
        self.dropped = 0
        self.last_duration = 0.0

    # ---- Schnittstelle fuer die Aufnahme -------------------------------
    def submit(self, entry_id: int, image_bytes: bytes) -> bool:
        try:
            self._queue.put_nowait((entry_id, image_bytes))
        except queue.Full:
            self.dropped += 1
            return False
        with self._inflight_lock:
            self._inflight.add(entry_id)
        return True

    def pending(self) -> int:
        return self._queue.qsize()

    # ---- Thread ----------------------------------------------------------
    def run(self) -> None:
        # Niedrige Prioritaet, aber KEIN Hintergrundmodus: der wuerde Tesseract auf Hybrid-CPUs
        # auf die Sparkerne zwingen und jede Seite in den Timeout laufen lassen (siehe winutil).
        winutil.set_thread_low_priority()
        while not self.stop_event.is_set():
            try:
                entry_id, png = self._queue.get(timeout=1.0)
            except queue.Empty:
                if self.backlog_enabled:
                    self._process_backlog()
                continue
            with self._inflight_lock:
                self._inflight.discard(entry_id)
            self._process(entry_id, png)

    def _process_backlog(self) -> None:
        now = time.monotonic()
        if now - self._last_backlog < BACKLOG_INTERVAL:
            return
        self._last_backlog = now
        try:
            candidates = self.storage.pending_ocr_ids(limit=10)
        except Exception:
            log.debug("pending_ocr_ids fehlgeschlagen", exc_info=True)
            return
        with self._inflight_lock:
            candidates = [c for c in candidates if c not in self._inflight]
        for entry_id in candidates:
            if self.stop_event.is_set():
                return
            row = self.storage.get_entry(entry_id)
            if row is None or row["ocr_status"] != 0:
                continue
            if time.time() * 1000 - row["created_at"] < BACKLOG_MIN_AGE_MS:
                continue
            webp = self.storage.get_image(entry_id)
            if webp is None:
                self.storage.mark_ocr_failed(entry_id)
                continue
            try:
                img = Image.open(io.BytesIO(webp))
                buf = io.BytesIO()
                img.convert("L").save(buf, "PNG", compress_level=1)
            except Exception:
                log.debug("Bild %d nicht dekodierbar", entry_id, exc_info=True)
                self.storage.mark_ocr_failed(entry_id)
                continue
            self._process(entry_id, buf.getvalue())
            return  # pro Intervall nur ein Nachhol-Job, um die CPU-Last flach zu halten

    def _process(self, entry_id: int, png: bytes) -> None:
        started = time.monotonic()
        try:
            result = self.engine.recognize(png)
        except OcrError as e:
            self.failed += 1
            log.warning("OCR fehlgeschlagen fuer Eintrag %d: %s", entry_id, e)
            try:
                self.storage.mark_ocr_failed(entry_id)
            except Exception:
                log.debug("mark_ocr_failed fehlgeschlagen", exc_info=True)
            return
        except Exception as e:
            self.failed += 1
            log.exception("Unerwarteter OCR-Fehler fuer Eintrag %d: %s", entry_id, e)
            return
        self.last_duration = time.monotonic() - started
        try:
            self.storage.set_ocr_result(entry_id, result.text or None, result.confidence, OCR_DONE)
        except Exception:
            log.debug("set_ocr_result fehlgeschlagen (Eintrag evtl. geloescht)", exc_info=True)
            return
        self.processed += 1
        log.debug("OCR Eintrag %d: %d Woerter in %.2fs", entry_id, result.words, self.last_duration)
        if self.on_result:
            try:
                self.on_result(entry_id, result)
            except Exception:
                log.debug("on_result-Callback fehlgeschlagen", exc_info=True)

    def stop(self) -> None:
        """Bricht einen laufenden Tesseract-Aufruf ab; der Thread endet ueber stop_event."""
        self.engine.kill()
