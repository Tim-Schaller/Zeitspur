"""Standort dieses PCs ueber die Ortung von Windows (Plugin "Windows-Standort").

Windows bestimmt den Standort aus GPS (falls eingebaut), den WLAN-Netzen in der Umgebung und der IP-Adresse;
fuer die WLAN-Ortung fragt Windows selbst den Ortungsdienst von Microsoft. Zeitspur liest nur das Ergebnis -
ueber System.Device.Location aus dem .NET Framework (pythonnet ist fuer das Fenster ohnehin geladen), alle
paar Minuten, solange der PC wach ist und die Aufnahme laeuft. Gespeichert werden Koordinaten und
Genauigkeit, verschluesselt; daraus werden Aufenthalte, die die Standort-Spur (location.py) mit WLAN und
Dawarich zusammenfuehrt.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import location
from .location import Point

if TYPE_CHECKING:  # pragma: no cover
    from .storage import Storage

log = logging.getLogger(__name__)

SOURCE = "windows_location"
DEFAULT_INTERVAL_MIN = 5
MAX_ACCURACY_M = 500.0             # ungenauer (IP-Ortung, Funkzelle) sagt nichts ueber den Ort
STAY_RADIUS_M = 150.0              # WLAN-Ortung schwankt staerker als GPS
MAX_GAP_MS = 20 * 60_000           # laengere Pause (Standby): die Luecke ist erschlossen, nicht belegt
RECOMPUTE_MS = 36 * 3_600_000      # so weit zurueck werden die Aufenthalte nach jeder Messung neu berechnet
SETTLE_MS = 60_000                 # nach dem (Wieder-)Start erst messen, wenn Windows neu geortet hat
SLEEP_GAP_MS = 120_000             # so lange ohne Beobachtung: der PC hat geschlafen

_CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location"


def _consent(hive, sub: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(hive, sub) as k:
            return str(winreg.QueryValueEx(k, "Value")[0])
    except OSError:
        return None


def access_reason() -> str | None:
    """Warum Zeitspur den Standort nicht lesen darf - oder None, wenn Windows es erlaubt."""
    try:
        import winreg
    except ImportError:   # pragma: no cover - nur unter Windows
        return "Nur unter Windows verfügbar."
    if _consent(winreg.HKEY_LOCAL_MACHINE, _CONSENT) == "Deny":
        return "Die Ortung ist auf diesem PC abgeschaltet (vom Administrator)."
    if _consent(winreg.HKEY_CURRENT_USER, _CONSENT) == "Deny":
        return ("Die Ortung ist in Windows ausgeschaltet: Einstellungen → Datenschutz und Sicherheit → Position → "
                "„Ortungsdienste“ einschalten.")
    if _consent(winreg.HKEY_CURRENT_USER, _CONSENT + r"\NonPackaged") == "Deny":
        return ("Desktop-Apps dürfen den Standort nicht verwenden: Einstellungen → Datenschutz und Sicherheit → "
                "Position → „Desktop-Apps den Zugriff auf Ihren Standort erlauben“ einschalten.")
    return None


@dataclass
class Fix:
    ts: int          # Zeitpunkt der Ortung (ms)
    lat: float
    lon: float
    acc: float | None


class Sampler:
    """Haelt einen GeoCoordinateWatcher am Laufen und liest bei Bedarf seine letzte Position."""

    def __init__(self) -> None:
        self._watcher = None
        self._closed = False
        self._lock = threading.Lock()

    def _ensure(self):
        if self._watcher is None:
            import clr  # pythonnet

            clr.AddReference("System.Device")
            from System.Device.Location import GeoCoordinateWatcher, GeoPositionAccuracy

            # "Default" statt "High": WLAN-Ortung reicht fuer Orte und schont den Akku (kein Dauer-GPS).
            watcher = GeoCoordinateWatcher(GeoPositionAccuracy.Default)
            watcher.Start(False)
            self._watcher = watcher
        return self._watcher

    def read(self) -> Fix | None:
        with self._lock:
            if self._closed:   # Plugin entfernt: keine neue Ortung mehr starten
                return None
            watcher = self._ensure()
            if str(watcher.Status) != "Ready":
                return None
            pos = watcher.Position
            loc = pos.Location
            if loc.IsUnknown:
                return None
            acc = float(loc.HorizontalAccuracy)
            return Fix(int(pos.Timestamp.ToUnixTimeMilliseconds()), float(loc.Latitude), float(loc.Longitude),
                       acc if acc == acc else None)   # NaN = unbekannt

    def wait_for_fix(self, timeout_s: float = 10.0) -> Fix | None:
        deadline = time.monotonic() + timeout_s
        while True:
            fix = self.read()
            if fix is not None or time.monotonic() >= deadline:
                return fix
            time.sleep(0.25)

    def close(self) -> None:
        """Endgueltig: Ortung beenden, auch ein spaeterer read() startet sie nicht wieder."""
        self.restart()
        with self._lock:
            self._closed = True

    def restart(self) -> None:
        with self._lock:
            if self._watcher is not None:
                try:
                    self._watcher.Stop()
                    self._watcher.Dispose()
                except Exception:   # pragma: no cover - .NET-Fehler beim Aufraeumen sind egal
                    log.debug("Watcher liess sich nicht sauber beenden", exc_info=True)
                self._watcher = None


def stays_from_points(points: list[Point]) -> list[dict]:
    """PC-Messpunkte -> Aufenthalte als calendar_events-Zeilen. Jede Messung zaehlt, auch eine einzelne."""
    pts = location.clean_points(points, max_accuracy_m=MAX_ACCURACY_M)
    rows: list[dict] = []
    for stay in location.detect_stays(pts, radius_m=STAY_RADIUS_M, min_ms=0):
        accs = [p.acc for p in pts[stay.i0:stay.i1 + 1] if p.acc is not None]
        for start, end in location.split_parts(pts, stay, MAX_GAP_MS):
            extra = {"latitude": round(stay.lat, 6), "longitude": round(stay.lon, 6), "points": stay.points}
            if accs:
                extra["accuracy_m"] = round(sorted(accs)[len(accs) // 2])
            rows.append({"ext_id": f"pc-{start}", "ts_start": start, "ts_end": end, "subject": "PC-Standort",
                         "location": location.fmt_coords(stay.lat, stay.lon), "organizer": None, "attendees": None,
                         "category": "visit", "extra": json.dumps(extra, ensure_ascii=False)})
    return rows


class LocationRecorder:
    """Misst alle interval_ms den Standort und schreibt die Aufenthalte fort."""

    def __init__(self, sampler: Sampler | None = None) -> None:
        self.sampler = sampler or Sampler()
        self._last_tick = 0
        self._next_sample = 0
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.sampler.close()

    def observe(self, storage: "Storage", now_ms: int, *, interval_ms: int, allowed: bool = True) -> int:
        slept = self._last_tick and now_ms - self._last_tick > SLEEP_GAP_MS
        first = not self._last_tick
        self._last_tick = now_ms
        if not allowed:
            return 0
        if first or slept:
            # Nach dem Aufwachen meldet Windows zuerst noch den alten Ort - erst neu orten lassen.
            if slept:
                self.sampler.restart()
            self._next_sample = now_ms + SETTLE_MS
            return 0
        if now_ms < self._next_sample:
            return 0
        self._next_sample = now_ms + interval_ms
        fix = self.sampler.read()
        if self.closed or fix is None or (fix.acc is not None and fix.acc > MAX_ACCURACY_M):
            return 0
        storage.add_location_point(SOURCE, now_ms, fix.lat, fix.lon, fix.acc)
        return self.recompute(storage, now_ms)

    @staticmethod
    def recompute(storage: "Storage", now_ms: int) -> int:
        start = now_ms - RECOMPUTE_MS
        # Ersetzt wird alles, was den Zeitraum beruehrt - ein Aufenthalt, der vor Tagen begann, muss aus
        # seinen eigenen Punkten neu entstehen, sonst waere sein Anfang weg.
        existing = storage.events_between(start, now_ms + 1, sources=[SOURCE])
        lo = min([start - 12 * 3_600_000] + [int(r["ts_start"]) for r in existing])
        rows = storage.location_points(SOURCE, lo, now_ms + 1)
        points = [Point(r["ts"], r["lat"], r["lon"], r["accuracy"]) for r in rows]
        events = [e for e in stays_from_points(points) if e["ts_end"] >= start]
        storage.replace_events(SOURCE, start, now_ms + 1, events)
        return len(events)


def describe_fix(fix: Fix | None) -> str:
    if fix is None:
        return "Windows liefert gerade keinen Standort – ist die Ortung eingeschaltet und hat der PC Empfang (WLAN)?"
    acc = f" (Genauigkeit ±{fix.acc:.0f} m)" if fix.acc is not None else ""
    if fix.acc is not None and fix.acc > MAX_ACCURACY_M:
        return f"Windows liefert nur einen ungenauen Standort{acc} – zu grob für Orte, solche Messungen werden verworfen."
    return f"Bereit – Windows liefert einen Standort{acc}."
