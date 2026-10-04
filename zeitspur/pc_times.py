"""PC-Zeiten aus dem System-Ereignisprotokoll: wann der PC an war - eingeschaltet, aus dem Standby geholt,
schlafen gelegt, heruntergefahren. Auch rueckwirkend und fuer Zeiten, in denen Zeitspur nicht lief.

Ereignisse (Anbieter, Id):
    Kernel-General 12 / 13          Systemstart / Herunterfahren (StartTime / StopTime)
    Kernel-Power 42 / 107           Ruhezustand bzw. Energiesparen / Aufwachen (klassisch, S3/S4)
    Power-Troubleshooter 1          zurueck aus dem Energiesparen, mit SleepTime und WakeTime
    Kernel-Power 506 / 507          Moderner Standby beginnt / endet (Notebooks, Bildschirm aus)
Ein Systemstart ohne vorheriges Herunterfahren heisst: Absturz oder Strom weg - die Zeit davor endet dann beim
letzten bekannten Ereignis. Sperren/Entsperren steht nur im Sicherheitsprotokoll (Adminrechte) und fehlt deshalb.
"""
from __future__ import annotations

import json
import logging

from . import eventlog, timeutil

log = logging.getLogger(__name__)

SOURCE = "pc_times"
LOOKBACK_MS = 2 * 86_400_000     # so weit vor dem Fenster suchen, um den Zustand an dessen Beginn zu kennen
MIN_PERIOD_MS = 2 * 60_000       # moderner Standby wacht oft fuer Sekunden auf, ohne dass jemand am PC ist
GENERAL = "Microsoft-Windows-Kernel-General"
POWER = "Microsoft-Windows-Kernel-Power"
TROUBLESHOOTER = "Microsoft-Windows-Power-Troubleshooter"
EVENT_IDS = [1, 12, 13, 42, 107, 506, 507]

_ON = {(GENERAL, 12): "Start", (POWER, 107): "Aufwachen", (POWER, 507): "Standby beendet"}
_OFF = {(GENERAL, 13): "Herunterfahren", (POWER, 42): "Ruhezustand", (POWER, 506): "Standby"}


def _time(event: eventlog.LogEvent, field: str) -> int:
    try:
        return eventlog.parse_systemtime(event.data[field])
    except (KeyError, ValueError):
        return event.ts_ms


def transitions(events: list[eventlog.LogEvent]) -> list[tuple[int, str, str]]:
    """Ereignisse -> sortierte Wechsel (Zeit, 'on'|'off', Grund)."""
    out: list[tuple[int, str, str]] = []
    for e in events:
        key = (e.provider, e.event_id)
        if key in _ON:
            out.append((_time(e, "StartTime") if key == (GENERAL, 12) else e.ts_ms, "on", _ON[key]))
        elif key in _OFF:
            out.append((_time(e, "StopTime") if key == (GENERAL, 13) else e.ts_ms, "off", _OFF[key]))
        elif key == (TROUBLESHOOTER, 1):
            out.append((_time(e, "SleepTime"), "off", "Ruhezustand"))
            out.append((_time(e, "WakeTime"), "on", "Aufwachen"))
    # Bei gleicher Zeit zuerst das Ende, dann der Beginn
    return sorted(out, key=lambda t: (t[0], 0 if t[1] == "off" else 1))


def periods(events: list[eventlog.LogEvent], now_ms: int) -> list[dict]:
    """Zeiten, in denen der PC an war, als Zeilen fuer calendar_events."""
    rows: list[dict] = []
    start: int | None = None
    reason = ""
    last = 0

    def emit(end_ms: int, end_reason: str | None) -> None:
        if start is None or end_ms - start < MIN_PERIOD_MS:
            return
        in_progress = end_reason is None
        subject = f"PC an (seit {reason})" if in_progress else f"PC an ({reason} → {end_reason})"
        rows.append({"ext_id": f"pc-{start}", "ts_start": start, "ts_end": end_ms, "subject": subject,
                     "location": None, "organizer": None, "attendees": None, "category": "pc_on",
                     "extra": json.dumps({"start_reason": reason, "end_reason": end_reason,
                                          "in_progress": in_progress}, ensure_ascii=False)})

    for ts, kind, why in transitions(events):
        if kind == "on":
            if start is not None and why == "Start":
                emit(last, "unerwartet beendet")   # neuer Systemstart ohne sauberes Ende davor
                start = None
            if start is None:
                start, reason = ts, why
        elif start is not None:
            emit(ts, why)
            start = None
        last = ts
    if start is not None:
        emit(max(now_ms, last), None)
    return rows


def fetch_periods(window_start: int, window_end: int) -> list[dict]:
    try:
        events = eventlog.query("System", EVENT_IDS, window_start - LOOKBACK_MS, window_end)
    except OSError as e:
        from .plugins import PluginError
        raise PluginError(str(e)) from e
    rows = periods(events, min(timeutil.now_ms(), window_end))
    return [r for r in rows if r["ts_end"] >= window_start and r["ts_start"] < window_end]
