"""Synchronisiert die Ereignisse der installierten Plugins (Termine, Anrufe, Orte) in die Datenbank.

Welche Quellen es gibt, steht in plugins.py; welche aktiv sind, in config.yaml (installed_plugins).
Alle landen in der Tabelle calendar_events und werden vom Zeitstrahl und vom MCP-Server gelesen.
Die Synchronisierung ersetzt je Quelle das synchronisierte Zeitfenster (idempotent), laeuft periodisch in
einem eigenen Thread mit niedriger Prioritaet und kann fuer einen einzelnen Tag angestossen werden, wenn im
Zeitstrahl ein Tag ausserhalb des rollierenden Fensters geoeffnet wird.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from datetime import date, timedelta

from . import plugins, timeutil, winutil
from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    counts: dict[str, int] = field(default_factory=dict)   # Plugin-Id -> Anzahl Ereignisse
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def merge(self, other: "SyncReport") -> None:
        for pid, n in other.counts.items():
            self.counts[pid] = self.counts.get(pid, 0) + n
        self.errors.extend(other.errors)

    def summary(self) -> str:
        teile = [f"{n} {plugins.get(pid).name}" for pid, n in self.counts.items()]
        text = ", ".join(teile) if teile else "keine Plugins installiert"
        return text + (f", Fehler: {'; '.join(self.errors)}" if self.errors else "")


class EventSync:
    """Fuehrt die eigentliche Synchronisierung aus (ohne Thread; direkt testbar)."""

    def __init__(self, cfg: Config, storage: Storage):
        self.cfg = cfg
        self.storage = storage

    def sync_range(self, start: date, end: date, *, sources: list[str] | None = None) -> SyncReport:
        report = SyncReport()
        window_start = timeutil.day_bounds(start)[0]
        window_end = timeutil.day_bounds(end)[1]
        ctx = plugins.SyncContext(self.cfg, self.storage, window_start, window_end)
        for plugin in plugins.installed(self.cfg):
            if plugin.observes or (sources is not None and plugin.id not in sources):
                continue   # beobachtende Plugins schreiben selbst mit (PluginObserverThread)
            reason = plugin.unavailable_reason() or plugin.not_ready_reason(self.cfg)
            if reason:
                report.errors.append(f"{plugin.name}: {reason}")
                continue
            try:
                rows = plugin.fetch(ctx, start, end)
                if plugin.id not in self.cfg.installed_plugins:
                    continue   # waehrend des Abrufs entfernt - seine Daten sind geloescht und bleiben es
                report.counts[plugin.id] = self.storage.replace_events(plugin.id, window_start, window_end, rows)
            except plugin.expected_errors() as e:
                # Ein Plugin, dessen Dienst gerade nicht erreichbar ist, darf die anderen nicht aufhalten.
                log.warning("%s: Sync fehlgeschlagen: %s", plugin.name, e)
                report.errors.append(f"{plugin.name}: {e}")
            except Exception as e:
                log.exception("%s: unerwarteter Fehler beim Sync", plugin.name)
                report.errors.append(f"{plugin.name}: {e}")
        return report

    def sync_window(self) -> SyncReport:
        """Rollierendes Fenster um heute (events_window_days rueckwaerts/vorwaerts)."""
        today = date.today()
        w = self.cfg.events_window_days
        return self.sync_range(today - timedelta(days=w), today + timedelta(days=w))

    def sync_day(self, day: date) -> SyncReport:
        return self.sync_range(day, day)

    def any_enabled(self) -> bool:
        """Gibt es ein installiertes Plugin, das abgerufen werden muss?"""
        return any(not p.observes for p in plugins.installed(self.cfg))


class EventSyncThread(threading.Thread):
    """Periodische Synchronisierung; zusaetzlich koennen einzelne Tage on-demand angefordert werden."""

    def __init__(self, cfg: Config, storage: Storage, stop_event: threading.Event, *,
                 initial_delay_s: float = 15.0):
        super().__init__(name="events", daemon=True)
        self.cfg = cfg
        self.storage = storage
        self.stop_event = stop_event
        self.initial_delay_s = initial_delay_s
        self.sync = EventSync(cfg, storage)
        self.last_report: SyncReport | None = None
        self.last_sync_ms: int | None = None
        self.runs = 0
        self._wake = threading.Event()
        self._requested_days: set[date] = set()
        self._lock = threading.Lock()

    def request_day(self, day: date) -> None:
        """Fordert eine Synchronisierung fuer einen bestimmten Tag an (z. B. beim Oeffnen im Zeitstrahl).

        Tage innerhalb des rollierenden Fensters deckt der periodische Sync bereits ab; dafuer wird der
        Thread NICHT geweckt (sonst laeuft z. B. die 30-s-Aktualisierung von "heute" jedes Mal einen vollen
        Outlook-/Teams-Sync an). Nur Tage ausserhalb des Fensters loesen einen zusaetzlichen Sync aus.
        """
        if not self.sync.any_enabled():
            return
        w = self.cfg.events_window_days
        if date.today() - timedelta(days=w) <= day <= date.today() + timedelta(days=w):
            return
        with self._lock:
            self._requested_days.add(day)
        self._wake.set()

    def wake(self) -> None:
        """Beendet die Wartephase sofort (z. B. beim Herunterfahren, damit der Thread schnell endet)."""
        self._wake.set()

    def run(self) -> None:
        # Der Thread laeuft auch ohne installiertes Plugin weiter und wartet: Plugins lassen sich zur
        # Laufzeit hinzufuegen (siehe trigger()), und ein beendeter Thread wuerde sie bis zum naechsten
        # Programmstart nie abholen.
        winutil.set_thread_background_mode()
        if self.stop_event.wait(self.initial_delay_s):
            return
        while not self.stop_event.is_set():
            if self.sync.any_enabled():
                try:
                    self._run_once()
                except Exception:
                    log.exception("Ereignis-Sync fehlgeschlagen")
            self._wake.wait(self.cfg.events_sync_minutes * 60)
            self._wake.clear()

    def trigger(self) -> None:
        """Sofort synchronisieren - z. B. nachdem ein Plugin hinzugefuegt oder eingerichtet wurde."""
        self._wake.set()

    def _run_once(self) -> None:
        with self._lock:
            days = self._requested_days
            self._requested_days = set()
        report = self.sync.sync_window()
        # Angeforderte Tage, die das rollierende Fenster ohnehin abdeckt, nicht erneut synchronisieren
        today = date.today()
        w = self.cfg.events_window_days
        outside = [d for d in sorted(days) if not (today - timedelta(days=w) <= d <= today + timedelta(days=w))]
        for day in outside:
            if self.stop_event.is_set():
                break
            report.merge(self.sync.sync_day(day))
        self.last_report = report
        self.last_sync_ms = timeutil.now_ms()
        self.runs += 1
        if report.total or report.errors:
            log.info("Ereignis-Sync: %s", report.summary())


OBSERVE_INTERVAL_S = 5.0
OBSERVE_ERROR_LOG_S = 300.0


class PluginObserverThread(threading.Thread):
    """Gibt beobachtenden Plugins alle paar Sekunden Gelegenheit, mitzuschreiben.

    Laeuft immer (auch ohne solches Plugin) und kostet dann nichts: Bei jedem Takt wird nur nachgesehen,
    ob eines installiert ist. So wirkt ein zur Laufzeit hinzugefuegtes Plugin beim naechsten Takt.
    `paused` meldet die pausierte Aufnahme - dann lesen Plugins keine Bildschirminhalte (Fenstertitel).
    """

    def __init__(self, cfg: Config, storage: Storage, stop_event: threading.Event, *,
                 interval_s: float = OBSERVE_INTERVAL_S, paused: Callable[[], bool] = lambda: False):
        super().__init__(name="observe", daemon=True)
        self.cfg = cfg
        self.storage = storage
        self.stop_event = stop_event
        self.interval_s = interval_s
        self.paused = paused
        self._last_error_log = -OBSERVE_ERROR_LOG_S

    def run(self) -> None:
        winutil.set_thread_low_priority()
        while not self.stop_event.wait(self.interval_s):
            self.tick()

    def tick(self) -> int:
        written = 0
        for plugin in plugins.installed(self.cfg):
            if not plugin.observes:
                continue
            try:
                if plugin.unavailable_reason():
                    continue
                written += plugin.observe(self.cfg, self.storage, timeutil.now_ms(),
                                          titles_allowed=not self.paused())
            except Exception:
                # Alle 5 s denselben Fehler zu protokollieren, wuerde das Log fluten.
                now = time.monotonic()
                if now - self._last_error_log >= OBSERVE_ERROR_LOG_S:
                    self._last_error_log = now
                    log.exception("%s: Beobachtung fehlgeschlagen", plugin.name)
        return written
