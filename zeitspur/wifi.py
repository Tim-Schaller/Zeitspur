"""WLAN-Netze aus dem WLAN-Protokoll von Windows: mit welchem Funknetz der PC wann verbunden war.

Ereignisse (WLAN-AutoConfig/Operational): 8001 verbunden, 8003 getrennt - je mit SSID, Profilname, Schnittstelle
und einer Verbindungs-Id, ueber die Beginn und Ende zusammenfinden. Fehlt das Ende (Herunterfahren, Absturz),
endet die Verbindung mit der naechsten Verbindung derselben Schnittstelle bzw. mit Herunterfahren oder Systemstart.

Mit einer Zuordnung "Netzname = Ort" wird daraus ein Ortshinweis ohne GPS - etwa "Buero (WLAN Firma)".
"""
from __future__ import annotations

import json
import logging
import re

from . import eventlog, timeutil

log = logging.getLogger(__name__)

SOURCE = "wifi"
CHANNEL = "Microsoft-Windows-WLAN-AutoConfig/Operational"
LOOKBACK_MS = 2 * 86_400_000
MIN_CONNECTION_MS = 30_000
MERGE_GAP_MS = 5 * 60_000      # kuerzere Unterbrechungen im selben Netz gelten als eine Verbindung
_SYSTEM_STOPS = [12, 13, 42]   # Systemstart, Herunterfahren, Ruhezustand: eine offene Verbindung endet dort


NO_PLACE = "-"   # "Handy-Hotspot = -": dieses Netz sagt nichts ueber den Ort


def ssid_key(ssid: str) -> str:
    """Vergleichsform eines Netznamens: Gross-/Kleinschreibung, Leer- und Bindestriche zaehlen nicht -
    'Firma-WLAN' in der Zuordnung trifft das Netz 'FirmaWLAN'."""
    text = (ssid or "").casefold()
    return re.sub(r"[\W_]+", "", text) or text.strip()   # nur Emoji/Satzzeichen: so lassen, wie es ist


def parse_places(lines: list[str]) -> dict[str, str]:
    """['Firma-WLAN = Buero', ...] -> {'firmawlan': 'Buero'}. Wirft ValueError bei kaputten Zeilen."""
    places: dict[str, str] = {}
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        ssid, sep, place = line.partition("=")
        if not sep or not ssid.strip() or not place.strip():
            raise ValueError(f"Zeile {n}: erwartet „Netzname = Ort“")
        places[ssid_key(ssid)] = place.strip()
    return places


def connections(events: list[eventlog.LogEvent], stops: list[int], now_ms: int,
                places: dict[str, str] | None = None) -> list[dict]:
    """WLAN-Ereignisse (+ Zeitpunkte von Systemstart/Herunterfahren) -> Verbindungen als Zeilen."""
    places = places or {}
    open_by_iface: dict[str, tuple[int, eventlog.LogEvent]] = {}
    rows: list[dict] = []
    stops = sorted(stops)

    def close(ts_start: int, ev: eventlog.LogEvent, ts_end: int, in_progress: bool = False) -> None:
        # Fehlt das Ende, nicht ueber ein Herunterfahren/einen Neustart hinaus verlaengern
        cut = next((s for s in stops if ts_start < s < ts_end), None)
        if cut is not None:
            ts_end, in_progress = cut, False
        if ts_end - ts_start < MIN_CONNECTION_MS:
            return
        ssid = ev.data.get("SSID") or ev.data.get("ProfileName") or "?"
        place = places.get(ssid_key(ssid))
        if place == NO_PLACE:
            place = None
        subject = f"{place} (WLAN {ssid})" if place else f"WLAN: {ssid}"
        extra = {"ssid": ssid, "profile": ev.data.get("ProfileName") or None, "in_progress": in_progress}
        if place:
            extra["place"] = place
        rows.append({"ext_id": f"wifi-{ev.data.get('ConnectionId', '')}-{ts_start}", "ts_start": ts_start,
                     "ts_end": ts_end, "subject": subject, "location": place or ssid, "organizer": None,
                     "attendees": None, "category": "wifi", "extra": json.dumps(extra, ensure_ascii=False)})

    for ev in sorted(events, key=lambda e: e.ts_ms):
        iface = ev.data.get("InterfaceGuid", "")
        if ev.event_id == 8001:
            if iface in open_by_iface:   # neue Verbindung ohne Trennung davor
                start, prev = open_by_iface.pop(iface)
                close(start, prev, ev.ts_ms)
            open_by_iface[iface] = (ev.ts_ms, ev)
        elif ev.event_id == 8003 and iface in open_by_iface:
            start, prev = open_by_iface[iface]
            if prev.data.get("ConnectionId") == ev.data.get("ConnectionId") or not ev.data.get("ConnectionId"):
                open_by_iface.pop(iface)
                close(start, prev, ev.ts_ms)
    for start, prev in open_by_iface.values():
        close(start, prev, max(now_ms, start), in_progress=True)
    return _merge(sorted(rows, key=lambda r: r["ts_start"]))


def _merge(rows: list[dict], gap_ms: int = MERGE_GAP_MS) -> list[dict]:
    """Kurze Unterbrechungen im selben Netz (Standby, Funkloch) zu einer Verbindung zusammenfassen."""
    merged: list[dict] = []
    for row in rows:
        if merged:
            prev = merged[-1]
            prev_extra, extra = json.loads(prev["extra"]), json.loads(row["extra"])
            if prev_extra["ssid"] == extra["ssid"] and row["ts_start"] - prev["ts_end"] <= gap_ms:
                prev["ts_end"] = max(prev["ts_end"], row["ts_end"])
                prev_extra["in_progress"] = extra["in_progress"]
                prev["extra"] = json.dumps(prev_extra, ensure_ascii=False)
                continue
        merged.append(dict(row))
    return merged


def fetch_connections(window_start: int, window_end: int, places: dict[str, str]) -> list[dict]:
    from .plugins import PluginError

    try:
        events = eventlog.query(CHANNEL, [8001, 8003], window_start - LOOKBACK_MS, window_end)
    except OSError as e:
        raise PluginError(str(e)) from e
    try:
        stops = [e.ts_ms for e in eventlog.query("System", _SYSTEM_STOPS, window_start - LOOKBACK_MS, window_end)
                 if e.provider.endswith(("Kernel-General", "Kernel-Power"))]
    except OSError:
        stops = []
    rows = connections(events, stops, min(timeutil.now_ms(), window_end), places)
    return [r for r in rows if r["ts_end"] >= window_start and r["ts_start"] < window_end]
