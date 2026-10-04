"""Wartung: Retention (14 Tage), Groessendeckel, Speicher an das Dateisystem zurueckgeben.

Loeschen laeuft in kleinen Batches mit Pausen; nach jedem Batch gibt `PRAGMA incremental_vacuum`
die frei gewordenen Seiten zurueck (auto_vacuum=INCREMENTAL, siehe crypto.open_database).
Ein vollstaendiges VACUUM ist nicht noetig und wird hier bewusst nicht ausgefuehrt.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import timeutil, winutil
from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 600            # Pruefintervall des Wartungs-Threads
INITIAL_DELAY_S = 900.0           # erst 15 min nach dem Start - die Anmeldephase bleibt frei
BUSY_IDLE_S = 90.0                # so kurz untaetig = Nutzer arbeitet gerade, Wartung wartet
MAX_DEFER_S = 6 * 3600.0          # laenger als das wird nicht verschoben
CLEANUP_PERIOD_MS = 24 * 3_600_000
FTS_OPTIMIZE_PERIOD_MS = 7 * 24 * 3_600_000
BATCH_SIZE = 200
BATCH_PAUSE_S = 0.2
GB = 1024 ** 3


@dataclass
class CleanupReport:
    deleted_retention: int = 0
    deleted_size_cap: int = 0
    pages_freed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    duration_s: float = 0.0
    aborted: bool = False
    backup: str = ""     # Dateiname der geschriebenen Sicherung (leer = keine)

    @property
    def bytes_freed(self) -> int:
        return max(0, self.bytes_before - self.bytes_after)

    @property
    def deleted(self) -> int:
        return self.deleted_retention + self.deleted_size_cap


def _logical_size(storage: Storage) -> int:
    st = storage.stats()
    return (st["page_count"] - st["freelist_count"]) * st["page_size"]


def make_backup(storage: Storage, cfg: Config) -> str | None:
    """Taegliche, konsistente Sicherung der Datenbank; behaelt die juengsten `backup_count` Staende.

    Hintergrund: Ist die Datei einmal nicht mehr entschluesselbar, ist ohne Sicherung der gesamte
    Verlauf weg. Mit Sicherung kostet ein solcher Schaden hoechstens einen Tag.
    """
    count = int(getattr(cfg, "backup_count", 0) or 0)
    if count <= 0:
        return None
    size = storage.db_size_bytes()
    limit = float(getattr(cfg, "backup_max_gb", 5.0)) * GB
    if limit and size > limit:
        log.info("Sicherung uebersprungen: Datenbank %.1f GB groesser als das Limit %.1f GB",
                 size / GB, limit / GB)
        return None
    backup_dir = Path(storage.db_path).parent / "backups"
    try:
        free = shutil.disk_usage(str(backup_dir.parent)).free
    except OSError:
        free = size * 3
    if free < size * 1.5:
        log.warning("Sicherung uebersprungen: zu wenig freier Speicherplatz")
        return None
    name = f"zeitspur-{time.strftime('%Y%m%d')}.db"
    if (backup_dir / name).exists():
        return None  # fuer heute schon gesichert
    try:
        written = storage.backup_to(backup_dir / name)
    except Exception:
        log.exception("Sicherung fehlgeschlagen")
        return None
    # Rotation: nur die juengsten `count` Staende behalten
    for old in sorted(backup_dir.glob("zeitspur-*.db"), key=lambda f: f.name, reverse=True)[count:]:
        try:
            old.unlink()
        except OSError:
            log.debug("Alte Sicherung %s nicht loeschbar", old.name, exc_info=True)
    log.info("Sicherung geschrieben: %s (%.1f MB)", name, written / 1e6)
    return name


def run_cleanup(storage: Storage, cfg: Config, stop_event: threading.Event | None = None, *,
                now_ms: int | None = None, batch_size: int = BATCH_SIZE, pause_s: float = BATCH_PAUSE_S) -> CleanupReport:
    """Einmaliger Aufraeumlauf (synchron). Wird vom Wartungs-Thread und von Tests genutzt."""
    started = time.monotonic()
    now = now_ms if now_ms is not None else timeutil.now_ms()
    report = CleanupReport(bytes_before=storage.db_size_bytes())
    stop = stop_event or threading.Event()

    def wait_pause() -> bool:
        return stop.wait(pause_s) if pause_s > 0 else stop.is_set()

    # 1) Retention
    cutoff = now - cfg.retention_days * timeutil.MS_PER_DAY
    while not stop.is_set():
        deleted = storage.delete_older_than(cutoff, batch_size)
        if deleted == 0:
            break
        report.deleted_retention += deleted
        report.pages_freed += storage.incremental_vacuum()
        if wait_pause():
            break

    # 2) Groessendeckel: aelteste Eintraege loeschen, bis die Nutzgroesse unter dem Limit liegt
    limit = int(cfg.max_db_size_gb * GB)
    warned = False
    while not stop.is_set() and _logical_size(storage) > limit:
        oldest, _ = storage.bounds()
        if oldest is None:
            break
        if not warned and now - oldest < timeutil.MS_PER_DAY:
            log.warning("max_db_size_gb=%.1f ist so klein, dass Eintraege des laufenden Tages geloescht werden",
                        cfg.max_db_size_gb)
            warned = True
        deleted = storage.delete_oldest(batch_size)
        if deleted == 0:
            break
        report.deleted_size_cap += deleted
        report.pages_freed += storage.incremental_vacuum()
        if wait_pause():
            break

    # 3) Kalender-/Anruf-Ereignisse aelter als die Aufbewahrungsfrist entfernen
    while not stop.is_set():
        deleted = storage.delete_events_older_than(cutoff, batch_size)
        if deleted == 0:
            break
        if wait_pause():
            break
    # 3b) Teams-Klassifizierungs-Cache mitbereinigen
    while not stop.is_set():
        if storage.delete_teams_seen_older_than(cutoff, batch_size) == 0:
            break
        if wait_pause():
            break

    report.aborted = stop.is_set()
    # 4) Rest der Freelist zurueckgeben und WAL zusammenfalten, damit die Datei tatsaechlich schrumpft
    report.pages_freed += storage.incremental_vacuum()
    busy, _, _ = storage.checkpoint("TRUNCATE")
    if busy:
        log.info("WAL-Checkpoint konnte nicht vollstaendig ausgefuehrt werden (Leser aktiv)")
    report.bytes_after = storage.db_size_bytes()
    report.duration_s = time.monotonic() - started

    if not report.aborted:
        report.backup = make_backup(storage, cfg) or ""
        storage.set_meta("last_cleanup", str(now))
    total = int(storage.get_meta("bytes_freed_total", "0") or 0) + report.bytes_freed
    storage.set_meta("bytes_freed_total", str(total))
    storage.set_meta("last_cleanup_report",
                     f"deleted={report.deleted} freed={report.bytes_freed} duration={report.duration_s:.1f}s")
    log.info("Aufraeumen: %d Eintraege geloescht (Retention %d, Groessendeckel %d), %.1f MB freigegeben, %.1f s%s",
             report.deleted, report.deleted_retention, report.deleted_size_cap, report.bytes_freed / 1e6,
             report.duration_s, " (abgebrochen)" if report.aborted else "")
    return report


def is_cleanup_due(storage: Storage, now_ms: int | None = None) -> bool:
    now = now_ms if now_ms is not None else timeutil.now_ms()
    last = storage.get_meta("last_cleanup")
    return last is None or now - int(last) >= CLEANUP_PERIOD_MS


def is_fts_optimize_due(storage: Storage, now_ms: int | None = None) -> bool:
    now = now_ms if now_ms is not None else timeutil.now_ms()
    last = storage.get_meta("last_fts_optimize")
    return last is None or now - int(last) >= FTS_OPTIMIZE_PERIOD_MS


class MaintenanceJob(threading.Thread):
    """Hintergrund-Thread: prueft periodisch, ob Aufraeumen/FTS-Optimierung faellig sind."""

    def __init__(self, cfg: Config, storage: Storage, stop_event: threading.Event, *,
                 initial_delay_s: float = INITIAL_DELAY_S, check_interval_s: float = CHECK_INTERVAL_S,
                 busy_idle_s: float = BUSY_IDLE_S, max_defer_s: float = MAX_DEFER_S,
                 on_report: Callable[[CleanupReport], None] | None = None):
        super().__init__(name="maintenance", daemon=True)
        self.cfg = cfg
        self.storage = storage
        self.stop_event = stop_event
        self.initial_delay_s = initial_delay_s
        self.check_interval_s = check_interval_s
        self.busy_idle_s = busy_idle_s
        self.max_defer_s = max_defer_s
        self._deferred_since: float | None = None
        self.on_report = on_report
        self.last_report: CleanupReport | None = None
        self.runs = 0
        self._wake = threading.Event()

    def trigger(self) -> None:
        """Naechste Pruefung sofort ausfuehren (z. B. nach Konfigurationsaenderung)."""
        self._wake.set()

    def wake(self) -> None:
        """Wartephase sofort beenden (beim Herunterfahren, damit der Thread nicht im sleep haengt)."""
        self._wake.set()

    def run(self) -> None:
        winutil.set_thread_background_mode()
        if self.stop_event.wait(self.initial_delay_s):
            return
        while not self.stop_event.is_set():
            try:
                self._check()
            except Exception:
                log.exception("Wartungslauf fehlgeschlagen")
            self._wake.wait(self.check_interval_s)
            self._wake.clear()

    def _postpone_while_busy(self) -> bool:
        """True, solange der Nutzer aktiv ist - Sicherung und Aufraeumen warten dann.

        Die taegliche Sicherung schreibt die komplette Datenbank neu (bei 224 MB gut 235 MB).
        Faellt das mit dem Anmelden oder konzentriertem Arbeiten zusammen, haengt der Rechner
        spuerbar. Nach spaetestens `max_defer_s` laeuft die Wartung trotzdem, damit sie nicht
        bei durchgehender Nutzung ganz ausfaellt.
        """
        try:
            idle = winutil.idle_seconds()
        except Exception:
            return False
        now = time.monotonic()
        if idle >= self.busy_idle_s:
            self._deferred_since = None
            return False
        if self._deferred_since is None:
            self._deferred_since = now
            return True
        if now - self._deferred_since >= self.max_defer_s:
            self._deferred_since = None
            log.info("Wartung laeuft trotz Aktivitaet (seit %.0f min verschoben)",
                     self.max_defer_s / 60)
            return False
        return True

    def _check(self) -> None:
        if self._postpone_while_busy():
            log.debug("Wartung verschoben, Nutzer ist aktiv")
            return
        # Sicherung zuerst und unabhaengig vom Aufraeumtakt: sonst gaebe es nach einem Neustart
        # bis zu 24 h lang keinen Sicherungsstand. make_backup ueberspringt, was heute schon lief.
        try:
            make_backup(self.storage, self.cfg)
        except Exception:
            log.exception("Sicherung fehlgeschlagen")
        if is_cleanup_due(self.storage):
            report = run_cleanup(self.storage, self.cfg, self.stop_event)
            self.last_report = report
            self.runs += 1
            if report.aborted:
                return
            reset = self.storage.reset_failed_ocr()
            if reset:
                log.info("%d fehlgeschlagene OCR-Eintraege werden erneut versucht", reset)
            if self.on_report:
                try:
                    self.on_report(report)
                except Exception:
                    log.debug("on_report-Callback fehlgeschlagen", exc_info=True)
        if not self.stop_event.is_set() and is_fts_optimize_due(self.storage):
            self.storage.optimize_fts()
            self.storage.set_meta("last_fts_optimize", str(timeutil.now_ms()))
            log.info("FTS-Index optimiert")
