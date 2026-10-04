"""Standort-Historie aus einer privaten Dawarich-Instanz (schreibgeschuetzte HTTPS-Schnittstelle).

Bevorzugt holt Zeitspur die GPS-Rohpunkte (/api/v1/points) und berechnet Aufenthalte und Fahrten selbst
(location.segment_points): genaue Zeiten, Koordinaten fuer jeden Ort, Strecke und Entfernung je Fahrt.
Dawarichs eigene Aufenthalte (/api/v1/visits) liefern dann nur noch die Namen. Ist /api/v1/points nicht
erreichbar (etwa weil ein vorgeschalteter Proxy nur visits/tracks durchlaesst), faellt das Plugin auf die
beiden alten Endpunkte zurueck - mit deren Schwaechen (Luecken, Ganztages-"Fahrten", Orte ohne Koordinaten).

Die Ergebnisse landen als Belege in calendar_events (source='dawarich'); die Standort-Spur (location.py)
fuehrt sie mit WLAN und Windows-Standort zu einer lueckenlosen Leiste zusammen.

Sicherheit: Basis-Adresse und Token liegen per DPAPI geschuetzt in
%LOCALAPPDATA%\\Zeitspur\\dawarich_credentials.bin - nie im Klartext-config.yaml. Der Token geht
ausschliesslich in den Authorization-Header, nie in die URL (URLs landen in Proxy-Logs) und niemals in
unsere Protokolle. Unverschluesseltes http:// wird abgelehnt.

Netzwerkzugriff bewusst nur ueber die Standardbibliothek (urllib), wie bei den anderen Quellen.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import location, timeutil, winutil
from .config import data_dir
from .location import Point, fmt_coords, fmt_km

if TYPE_CHECKING:  # pragma: no cover
    from .storage import Storage

log = logging.getLogger(__name__)

SOURCE = "dawarich"
DAWARICH_ENTROPY = b"Zeitspur-Dawarich-credentials-v1"   # darf sich nie aendern, sonst sind gespeicherte Zugangsdaten unlesbar
CREDENTIALS_FILE = "dawarich_credentials.bin"
HTTP_TIMEOUT = 30
PER_PAGE = 500          # Obergrenze der Schnittstelle
MAX_PAGES = 10          # Sicherheitsnetz gegen Endlosschleifen
POINTS_PER_PAGE = 1000
MAX_POINT_PAGES = 30    # 30 000 Punkte je Tag - mehr sendet kein Handy
# Ein Tag gilt als vollstaendig abgeholt, wenn das so lange nach Tagesende geschah: Handys laden
# Punkte verspaetet hoch (Funkloch, Energiesparen). Bis dahin wird er bei jedem Abgleich neu geholt.
FINAL_AFTER_MS = 6 * 3_600_000
POINTS_META_KEY = "dawarich.points_days"
# Aufenthalte werden nach started_at gefiltert: einer, der vor dem Fenster begann und hineinreicht,
# fiele sonst weg. Deshalb grosszuegig frueher abfragen und selbst auf Ueberlappung filtern.
VISIT_LOOKBACK_DAYS = 7
DECLINED = "declined"
MAX_PATH_POINTS = 500   # Obergrenze je Fahrt, damit die Datenbank nicht mit Rohpunkten volllaeuft
PATH_EPSILON = 2e-5     # ~2 m; feiner braucht die Kartendarstellung nicht


class DawarichError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# Obergrenze je Antwort gegen Speichererschoepfung durch einen boesartigen/kompromittierten Server.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Folgt Weiterleitungen nur auf https und entfernt bei Hostwechsel den Bearer-Token (kein Token an Fremdhosts)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        target = urllib.parse.urlsplit(new.full_url)
        if target.scheme.lower() != "https":
            raise DawarichError("Weiterleitung auf eine unverschluesselte Adresse abgelehnt.", code)
        if target.hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


_OPENER: urllib.request.OpenerDirector | None = None


def _opener() -> urllib.request.OpenerDirector:
    # Gleiche Haertung wie httpclient: truststore-TLS-Kontext (auch auf frischen Windows-PCs) + sichere Redirects.
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=winutil.https_context()), _SafeRedirect())
    return _OPENER


# --------------------------------------------------------------------------- Zugangsdaten (DPAPI)

def credentials_path() -> Path:
    return data_dir() / CREDENTIALS_FILE


def normalize_base_url(base_url: str) -> str:
    """Prueft und normalisiert die Basis-Adresse. Nur https - ueber http duerfte der Token nie gehen."""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Die Basis-Adresse fehlt.")
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("Nur https ist zulaessig - ueber http waere der Token im Klartext unterwegs.")
    if not parts.hostname:
        raise ValueError("Die Basis-Adresse enthaelt keinen Hostnamen.")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def save_credentials(base_url: str, token: str, path: Path | None = None) -> Path:
    from .crypto import dpapi_protect

    url = normalize_base_url(base_url)
    token = (token or "").strip()
    if not token:
        raise ValueError("Der Token fehlt.")
    payload = json.dumps({"base_url": url, "token": token}).encode("utf-8")
    p = path or credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".bin.tmp")
    tmp.write_bytes(dpapi_protect(payload, DAWARICH_ENTROPY, "Zeitspur Dawarich credentials"))
    import os

    os.replace(tmp, p)
    return p


def load_credentials(path: Path | None = None) -> dict | None:
    from .crypto import KeyProtectionError, dpapi_unprotect

    p = path or credentials_path()
    if not p.exists():
        return None
    try:
        data = dpapi_unprotect(p.read_bytes(), DAWARICH_ENTROPY)
        creds = json.loads(data.decode("utf-8"))
    except (KeyProtectionError, ValueError) as e:
        log.warning("Dawarich-Zugangsdaten nicht lesbar: %s", e)
        return None
    if not all(creds.get(k) for k in ("base_url", "token")):
        return None
    return creds


def clear_credentials(path: Path | None = None) -> bool:
    p = path or credentials_path()
    if p.exists():
        p.unlink()
        return True
    return False


def has_credentials(path: Path | None = None) -> bool:
    p = path or credentials_path()
    return p.exists()


# --------------------------------------------------------------------------- Abbildung (rein, testbar)

@dataclass
class LocationEvent:
    ext_id: str | None
    ts_start: int
    ts_end: int
    subject: str
    location: str | None
    organizer: str | None
    attendees: str | None
    category: str
    extra: str | None

    def as_row(self) -> dict:
        return self.__dict__.copy()


def _coords(lat, lon) -> str | None:
    try:
        return fmt_coords(float(lat), float(lon))
    except (TypeError, ValueError):
        return None


def _plausible_km(distance_raw, ts_start: int, ts_end: int) -> float | None:
    """`distance` als Meter deuten - aber nur uebernehmen, wenn das Ergebnis plausibel ist.

    Die Einheit ist seitens Dawarich nicht zugesichert. Eine "Fahrt ueber 3400 km" oder ein Schnitt
    jenseits von 400 km/h weist auf die falsche Einheit hin, nicht auf eine Reise - dann lieber gar
    keine Entfernung anzeigen als eine falsche.
    """
    try:
        km = float(distance_raw) / 1000.0
    except (TypeError, ValueError):
        return None
    if km <= 0 or km > 2000:
        return None
    hours = max(0, ts_end - ts_start) / 3_600_000
    if hours > 0 and km / hours > 400:
        return None
    return km


# --------------------------------------------------------------------------- Fortbewegungsarten
# Dawarich zerlegt jede Fahrt in Abschnitte und kennzeichnet jeden per Emoji (Feld mode_timeline).
MOTOR_EMOJI = frozenset({"🚗", "🚕", "🚙", "🚐", "🛻", "🚚", "🚌", "🚎", "🚆", "🚄", "🚊", "🚇",
                         "🚈", "🚝", "🚋", "🛵", "🏍", "🚢", "⛴", "✈", "🚁"})
BIKE_EMOJI = frozenset({"🚴", "🛴"})
ON_FOOT_EMOJI = frozenset({"🚶", "🏃"})
MODE_LABELS = {"driving": "Autofahrt", "car": "Autofahrt", "cycling": "Radfahrt", "bike": "Radfahrt",
               "walking": "Zu Fuß", "running": "Lauf", "public_transport": "Fahrt mit Öffis",
               "train": "Zugfahrt", "flight": "Flug", "boat": "Bootsfahrt"}
MIN_TRACK_M = 250.0     # darunter ist es keine Fahrt, sondern Stehen mit GPS-Rauschen
JOURNEY_GAP_S = 900.0   # 15 min ohne Motorbewegung trennen zwei Fahrten
EDGE_MAX_S = 300.0      # nur kurze Rad-/Fussabschnitte gehoeren noch zur Fahrt (Weg zum Auto)
EDGE_GAP_S = 180.0      # und nur, wenn sie unmittelbar anschliessen
MAX_TRACK_KMH = 200.0   # schneller ist die Entfernung nicht die der gezeigten Fahrt
EDGE_MATCH_MS = 15 * 60_000   # so nah am Anfang/Ende des Tracks gilt dessen Start/Ziel auch fuer die Fahrt


def _mode_kind(emoji: str | None) -> str:
    """'motor', 'rad', 'fuss' oder 'halt' (Stillstand bzw. unbekannt)."""
    e = (emoji or "").replace("️", "")  # Variantenselektor ignorieren: "✈️" == "✈"
    if e in MOTOR_EMOJI:
        return "motor"
    if e in BIKE_EMOJI:
        return "rad"
    if e in ON_FOOT_EMOJI:
        return "fuss"
    return "halt"


def _segments(props: dict) -> list[tuple[str, float, float]]:
    """mode_timeline als zeitlich sortierte Liste (Art, Beginn, Ende) in Unix-Sekunden."""
    raw = props.get("mode_timeline")
    if not isinstance(raw, list):
        return []
    out = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        a, b = s.get("start_time"), s.get("end_time")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b >= a:
            out.append((_mode_kind(s.get("emoji")), float(a), float(b)))
    out.sort(key=lambda t: t[1])
    return out


def _widen(segments: list[tuple[str, float, float]], von: float, bis: float, kern: str) -> tuple[float, float]:
    """Kurze Anschlussabschnitte mitnehmen - den Weg zum Auto, nicht den Aufenthalt danach."""
    while True:
        for art, a, b in segments:
            if art in (kern, "halt") or b - a > EDGE_MAX_S:
                continue
            if 0 <= von - b <= EDGE_GAP_S:
                von = a
                break
            if 0 <= a - bis <= EDGE_GAP_S:
                bis = b
                break
        else:
            return von, bis


def journeys(props: dict, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Zerlegt einen Dawarich-Track in die Fahrten, die wirklich stattgefunden haben.

    Dawarich fasst oft einen ganzen Tag zu einem einzigen Track zusammen: Hinfahrt, Arbeitstag im Buero
    und Rueckfahrt landen in einem Block von morgens bis abends, Start gleich Ziel. Verlaesslich sind
    daran nur die Zeiten der Motorabschnitte. Die uebrige Einstufung ist es nicht: Ein Handy, das
    stundenlang auf dem Schreibtisch liegt, gilt abschnittsweise als "Radfahren" oder "Laufen". Auch
    die Entfernung taugt nicht als Mass - ein grosser Teil kann aus GPS-Zittern im Stand stammen.

    Deshalb: Jede Gruppe von Motorabschnitten ist eine Fahrt. Liegen zwei weniger als
    JOURNEY_GAP_S auseinander, ist es eine Fahrt mit Zwischenstopp, sonst beginnt eine neue.
    An den Raendern kommen kurze Rad-/Fussabschnitte dazu (der Weg zum Auto), lange nicht.
    Enthaelt der Track keinen Motorabschnitt, gelten Rad- bzw. Fussabschnitte als Fortbewegung -
    dann ist es eine Radtour oder ein Spaziergang.
    """
    segments = _segments(props)
    kerne: list[tuple[float, float]] = []
    kern = ""
    for art in ("motor", "rad", "fuss"):
        kerne = [(a, b) for k, a, b in segments if k == art]
        if kerne:
            kern = art
            break
    if not kerne:
        return [(start_ms, end_ms)]
    gruppen: list[list[tuple[float, float]]] = [[kerne[0]]]
    for a, b in kerne[1:]:
        if a - gruppen[-1][-1][1] > JOURNEY_GAP_S:
            gruppen.append([(a, b)])
        else:
            gruppen[-1].append((a, b))
    ergebnis = []
    for gruppe in gruppen:
        von, bis = _widen(segments, gruppe[0][0], gruppe[-1][1], kern)
        s, e = max(start_ms, int(von * 1000)), min(end_ms, int(bis * 1000))
        if e > s:
            ergebnis.append((s, e))
    return ergebnis or [(start_ms, end_ms)]



def _path_from_line(line) -> list:
    """GeoJSON-LineString [[lon, lat], ...] -> [[lat, lon], ...], vereinfacht und gerundet."""
    pts = []
    for pair in line or []:
        try:
            pts.append((float(pair[1]), float(pair[0])))  # GeoJSON ist [lon, lat]
        except (TypeError, ValueError, IndexError):
            continue
    if not pts:
        return []
    return location.simplify_path(pts, PATH_EPSILON, MAX_PATH_POINTS)


def build_visit_event(visit: dict) -> LocationEvent | None:
    """Ein Aufenthalt -> Ereignis. None, wenn unbrauchbar (keine Zeit) oder vom Nutzer verworfen."""
    if str(visit.get("status", "")).lower() == DECLINED:
        return None  # verworfener Aufenthalt - nicht als Tatsache behandeln
    start = timeutil.iso_to_ms(visit.get("started_at"))
    if start is None:
        return None
    end = timeutil.iso_to_ms(visit.get("ended_at")) or start
    place = visit.get("place") or {}
    coords = _coords(place.get("latitude"), place.get("longitude"))
    name = (visit.get("name") or "").strip()
    # Ortsnamen fehlen bei Dawarich derzeit meist - dann ehrlich die Koordinaten nennen statt zu raten.
    subject = name or (f"Aufenthalt ({coords})" if coords else "Aufenthalt")
    extra = {k: visit.get(k) for k in ("status", "confidence", "confidence_band", "duration", "area_id")
             if visit.get(k) is not None}
    if name:
        extra["name"] = name
    if place.get("latitude") is not None:
        extra["latitude"] = place.get("latitude")
        extra["longitude"] = place.get("longitude")
    if place.get("id") is not None:
        extra["place_id"] = place.get("id")
    vid = visit.get("id")
    return LocationEvent(
        ext_id=f"visit-{vid}" if vid is not None else None,
        ts_start=int(start), ts_end=int(max(end, start)),
        subject=subject, location=coords, organizer=None, attendees=None,
        category="visit", extra=json.dumps(extra, ensure_ascii=False))


def build_track_events(feature: dict) -> list[LocationEvent]:
    """Ein GeoJSON-Feature (Dawarich-Track) -> ein Ereignis je tatsaechlich gefahrener Fahrt."""
    props = feature.get("properties") or {}
    track_start = timeutil.iso_to_ms(props.get("start_at"))
    if track_start is None:
        return []
    track_ende = max(timeutil.iso_to_ms(props.get("end_at")) or track_start, track_start)
    try:
        meter = float(props.get("distance"))
    except (TypeError, ValueError):
        meter = None
    if meter is not None and meter < MIN_TRACK_M:
        # Dawarich fuehrt auch reines Stehen als "Fahrt" (beobachtet: 10 m in 51 Minuten).
        # Als Balken im Zeitstrahl waere das schlicht falsch.
        return []
    fahrten = journeys(props, track_start, track_ende)
    geom = feature.get("geometry") or {}
    line = geom.get("coordinates") if geom.get("type") == "LineString" else None
    first = last = None
    first_pt = last_pt = None
    if isinstance(line, list) and line:
        # GeoJSON ist [Laengengrad, Breitengrad] - genau umgekehrt zur ueblichen Schreibweise.
        try:
            first_pt = [round(float(line[0][1]), 6), round(float(line[0][0]), 6)]
            last_pt = [round(float(line[-1][1]), 6), round(float(line[-1][0]), 6)]
            first, last = _coords(*first_pt), _coords(*last_pt)
        except (IndexError, TypeError, ValueError):
            first = last = first_pt = last_pt = None
    label = MODE_LABELS.get(str(props.get("dominant_mode") or "").lower(), "Fahrt")
    einzeln = len(fahrten) == 1
    tid = props.get("id")
    events = []
    for nr, (von, bis) in enumerate(fahrten, 1):
        # Entfernung nur bei einer einzigen Fahrt: Sie gilt fuer den ganzen Track und laesst sich
        # nicht auf mehrere Fahrten aufteilen - die Punkte tragen keine Zeitstempel, und eine
        # Aufteilung nach Zeit- oder Streckenanteil waere geraten. Lieber keine Angabe.
        km = _plausible_km(props.get("distance"), track_start, track_ende) if einzeln else None
        stunden = max(0, bis - von) / 3_600_000
        if km is not None and stunden > 0 and km / stunden > MAX_TRACK_KMH:
            km = None  # von Zittern im Stand aufgeblaehte Entfernung, passt nicht zur Fahrtzeit
        extra = {k: props.get(k) for k in ("distance", "duration", "avg_speed") if props.get(k) is not None}
        if props.get("dominant_mode"):
            extra["mode"] = props["dominant_mode"]
        if km is not None:
            extra["distance_km"] = round(km, 2)
        if (von, bis) != (track_start, track_ende):
            # distance, duration und avg_speed oben gelten fuer diesen ganzen Zeitraum, nicht fuer die Fahrt.
            extra["track_start"] = props.get("start_at")
            extra["track_end"] = props.get("end_at")
        if not einzeln:
            extra["journey"] = f"{nr}/{len(fahrten)}"
        # Start/Ziel: Anfang und Ende des Tracks - fuer die erste bzw. letzte Fahrt nur, wenn sie dort
        # (fast) beginnt bzw. endet. Damit kann die Standort-Spur die Orte davor und danach anschliessen.
        if first_pt and (einzeln or (nr == 1 and von - track_start <= EDGE_MATCH_MS)):
            extra["from"] = first_pt
        if last_pt and (einzeln or (nr == len(fahrten) and track_ende - bis <= EDGE_MATCH_MS)):
            extra["to"] = last_pt
        if isinstance(line, list):
            extra["points"] = len(line)
            path = _path_from_line(line)
            if path:
                # Die Strecke gehoert zum ganzen Track; einzelnen Fahrten laesst sie sich mangels
                # Zeitstempeln nicht zuordnen. Fuer die Karte ist der Tagesweg trotzdem nuetzlich.
                extra["path"] = path
                if not einzeln:
                    extra["path_covers_whole_track"] = True
        # Start und Ziel sind die Enden des ganzen Tracks - bei mehreren Fahrten waeren sie irrefuehrend.
        ort = None
        if einzeln:
            ort = f"{first} nach {last}" if first and last and first != last else (first or last)
        events.append(LocationEvent(
            ext_id=None if tid is None else (f"track-{tid}" if einzeln else f"track-{tid}-{nr}"),
            ts_start=int(von), ts_end=int(bis),
            subject=label + (f" - {fmt_km(km)}" if km is not None else ""),
            location=ort, organizer=None, attendees=None,
            category="track", extra=json.dumps(extra, ensure_ascii=False)))
    return events



# --------------------------------------------------------------------------- GPS-Rohpunkte

_WKT_POINT = re.compile(r"POINT\s*\(\s*([-0-9.eE]+)\s+([-0-9.eE]+)\s*\)")


def parse_point(raw: dict) -> Point | None:
    """Ein Punkt aus /api/v1/points -> Point. Robust gegen die Varianten der Dawarich-Versionen."""
    if not isinstance(raw, dict):
        return None
    lat, lon = raw.get("latitude"), raw.get("longitude")
    if (lat is None or lon is None) and isinstance(raw.get("lonlat"), str):
        m = _WKT_POINT.search(raw["lonlat"])   # neuere Versionen: "POINT (lon lat)"
        if m:
            lon, lat = m.group(1), m.group(2)
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    ts = raw.get("timestamp")
    if isinstance(ts, str) and ts.strip().lstrip("-").isdigit():
        ts = int(ts)
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        ms = int(ts * 1000) if ts < 100_000_000_000 else int(ts)   # Sekunden oder schon Millisekunden
    else:
        ms = timeutil.iso_to_ms(ts if isinstance(ts, str) else None)
    if ms is None:
        return None
    try:
        acc = float(raw["accuracy"]) if raw.get("accuracy") is not None else None
    except (TypeError, ValueError):
        acc = None
    return Point(ms, lat, lon, acc)


def _visit_spans(visits: list) -> list[tuple[int, int, str]]:
    """Dawarichs Aufenthalte als (Beginn, Ende, Name) - nur zum Benennen der eigenen Aufenthalte."""
    out = []
    for v in visits or []:
        if not isinstance(v, dict) or str(v.get("status", "")).lower() == DECLINED:
            continue
        name = (v.get("name") or "").strip()
        start = timeutil.iso_to_ms(v.get("started_at"))
        if not name or start is None:
            continue
        end = max(timeutil.iso_to_ms(v.get("ended_at")) or start, start)
        out.append((start, end, name))
    return out


def _name_for(start: int, end: int, spans: list[tuple[int, int, str]]) -> str | None:
    """Name des Dawarich-Aufenthalts, der am besten zur Zeit passt (mind. halb ueberlappend oder 10 Minuten)."""
    best, best_ov = None, 0
    for s, e, name in spans:
        ov = min(end, e) - max(start, s)
        shorter = max(1, min(end - start, e - s))
        if ov > best_ov and (ov >= shorter / 2 or ov >= 10 * 60_000):
            best, best_ov = name, ov
    return best


def events_from_points(points: list[Point], visits: list, window_start: int, window_end: int) -> list[dict]:
    """GPS-Rohpunkte -> Aufenthalte und Fahrten als calendar_events-Zeilen (nur was das Fenster beruehrt)."""
    stays, trips = location.segment_points(points)
    spans = _visit_spans(visits)
    rows: list[dict] = []
    for st in stays:
        name = _name_for(st.start, st.end, spans)
        for ps, pe in st.parts or [(st.start, st.end)]:
            extra = {"latitude": round(st.lat, 6), "longitude": round(st.lon, 6), "points": st.points,
                     "basis": "points"}
            if name:
                extra["name"] = name
            rows.append({"ext_id": f"stay-{ps}", "ts_start": ps, "ts_end": pe,
                         "subject": name or "Aufenthalt", "location": fmt_coords(st.lat, st.lon),
                         "organizer": None, "attendees": None, "category": "visit",
                         "extra": json.dumps(extra, ensure_ascii=False)})
    for tr in trips:
        km = tr.distance_m / 1000
        label = location.MODE_LABELS.get(tr.mode or "", "Fahrt")
        extra = {"distance_km": round(km, 2), "mode": tr.mode, "path": tr.path,
                 "from": [round(tr.from_pt[0], 6), round(tr.from_pt[1], 6)],
                 "to": [round(tr.to_pt[0], 6), round(tr.to_pt[1], 6)], "basis": "points"}
        if tr.max_kmh is not None:
            extra["max_kmh"] = round(tr.max_kmh)
        rows.append({"ext_id": f"trip-{tr.start}", "ts_start": tr.start, "ts_end": tr.end,
                     "subject": f"{label} - {fmt_km(km)}",
                     "location": f"{fmt_coords(*tr.from_pt)} nach {fmt_coords(*tr.to_pt)}",
                     "organizer": None, "attendees": None, "category": "track",
                     "extra": json.dumps(extra, ensure_ascii=False)})
    # Dawarichs eigene, benannte Orte mit Koordinaten ("Zuhause", "Firma"): kein Beleg fuer die Leiste, aber
    # die Standort-Spur benennt damit Orte, an denen das Handy kaum Punkte schickt (Stillstand).
    for v in visits or []:
        place = (v.get("place") or {}) if isinstance(v, dict) else {}
        name = (v.get("name") or "").strip() if isinstance(v, dict) else ""
        start = timeutil.iso_to_ms(v.get("started_at")) if isinstance(v, dict) else None
        if not name or start is None or place.get("latitude") is None or str(v.get("status", "")).lower() == DECLINED:
            continue
        end = max(timeutil.iso_to_ms(v.get("ended_at")) or start, start)
        extra = {"latitude": place.get("latitude"), "longitude": place.get("longitude"), "name": name}
        rows.append({"ext_id": f"name-{v.get('id')}", "ts_start": start, "ts_end": end, "subject": name,
                     "location": None, "organizer": None, "attendees": None, "category": "named_place",
                     "extra": json.dumps(extra, ensure_ascii=False)})
    return [r for r in rows if r["ts_end"] >= window_start and r["ts_start"] <= window_end]


def days_to_fetch(start: date, end: date, fetched: dict[str, int], now_ms: int) -> list[date]:
    """Tage, deren Punkte (neu) geholt werden muessen: noch nie oder nicht lange genug nach Tagesende."""
    today = timeutil.local_date(now_ms)
    out = []
    d = start
    while d <= min(end, today):
        done = fetched.get(d.isoformat())
        if not isinstance(done, int) or done < timeutil.day_bounds(d)[1] + FINAL_AFTER_MS:
            out.append(d)
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- Zugriff

def _http_message(code: int) -> str:
    """Klartextmeldung je Antwortcode - bewusst ohne Token und ohne Antwortkoerper."""
    if code == 401:
        return "Token wurde abgelehnt (401). Bitte den Token in den Einstellungen pruefen."
    if code == 404:
        return "Endpunkt nicht vorhanden (404)."
    if code == 429:
        return "Zu viele Anfragen (429) - der naechste Sync versucht es erneut."
    if code in (502, 504):
        return f"Der Dawarich-Server ist gerade nicht erreichbar ({code})."
    return f"Abruf fehlgeschlagen (HTTP {code})."


class DawarichClient:
    # Dawarich erlaubt 60 Anfragen je Minute - beim Nachholen vieler Tage deshalb mit Abstand fragen.
    MIN_REQUEST_INTERVAL_S = 1.05

    def __init__(self, credentials: dict):
        self.base_url = normalize_base_url(credentials["base_url"])
        self._token = credentials["token"]
        self._last_request = 0.0
        self.requests = 0

    @classmethod
    def from_stored(cls, path: Path | None = None) -> "DawarichClient | None":
        creds = load_credentials(path)
        return cls(creds) if creds else None

    def _request(self, path: str, params: dict):
        wait = self.MIN_REQUEST_INTERVAL_S - (time.monotonic() - self._last_request)
        if wait > 0 and self.requests:
            time.sleep(wait)
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {self._token}",  # nur im Header, nie in der URL
            "Accept": "application/json"})
        self.requests += 1
        try:
            with _opener().open(req, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise DawarichError(
                        f"Antwort des Standort-Dienstes ist zu gross (ueber {MAX_RESPONSE_BYTES // (1024 * 1024)} MB).")
                return json.loads(body.decode("utf-8")), resp.headers
        except urllib.error.HTTPError as e:
            raise DawarichError(_http_message(e.code), e.code) from e
        except urllib.error.URLError as e:
            raise DawarichError(f"Standort-Dienst nicht erreichbar: {e.reason}") from e
        except ValueError as e:
            raise DawarichError(f"Unerwartete Antwort: {e}") from e
        finally:
            self._last_request = time.monotonic()

    def _get(self, path: str, params: dict):
        return self._request(path, params)[0]

    def _pages(self, path: str, start_utc: datetime, end_utc: datetime, extract) -> list:
        """Seitenweise abfragen - grosse Zeitraeume am Stueck und hoechstens MAX_PAGES Seiten."""
        items: list = []
        for page in range(1, MAX_PAGES + 1):
            payload = self._get(path, {
                "start_at": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_at": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "page": page, "per_page": PER_PAGE})
            batch = extract(payload)
            items.extend(batch)
            if len(batch) < PER_PAGE:
                break
        else:
            log.warning("Dawarich %s: Seitenlimit erreicht, Ergebnis eventuell unvollstaendig", path)
        return items

    @staticmethod
    def _utc(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ---- GPS-Rohpunkte
    def points_available(self) -> bool:
        """Liefert der Server /api/v1/points? 404 heisst: nicht freigegeben (oft ein vorgeschalteter Proxy)."""
        now = timeutil.now_ms()
        try:
            self._request("/api/v1/points", {"start_at": self._utc(now - 3_600_000), "end_at": self._utc(now),
                                             "page": 1, "per_page": 1, "slim": "true"})
        except DawarichError as e:
            if e.status == 404:
                return False
            raise
        return True

    def fetch_points(self, start_ms: int, end_ms: int) -> list[Point]:
        """Alle Punkte in [start, end), zeitlich aufsteigend.

        Massgeblich fuer das Ende ist X-Total-Pages. Fehlt der Kopf, wird bis zur ersten leeren Seite
        geblaettert - eine kuerzere Seite heisst nicht "fertig", ein Server oder Proxy kann per_page deckeln.
        """
        points: list[Point] = []
        previous = None
        for page in range(1, MAX_POINT_PAGES + 1):
            payload, headers = self._request("/api/v1/points", {
                "start_at": self._utc(start_ms), "end_at": self._utc(end_ms - 1000),
                "page": page, "per_page": POINTS_PER_PAGE, "order": "asc", "slim": "true"})
            batch = payload if isinstance(payload, list) else (payload or {}).get("points") or []
            if not batch or batch[0] == previous:
                break   # leer - oder der Server ignoriert "page" und liefert immer dieselbe Seite
            previous = batch[0]
            points.extend(p for p in (parse_point(r) for r in batch) if p is not None and start_ms <= p.ts < end_ms)
            try:
                total = int(headers.get("X-Total-Pages") or 0)
            except (TypeError, ValueError):
                total = 0
            if total and page >= total:
                break
        else:
            log.warning("Dawarich: mehr als %d Punkte an einem Tag, Rest ausgelassen",
                        MAX_POINT_PAGES * POINTS_PER_PAGE)
        return points

    def fetch_visits(self, start_ms: int, end_ms: int) -> list:
        """Dawarichs eigene Aufenthalte (Rohdaten) - mit Vorlauf, weil der Server nach Beginn filtert."""
        start_utc = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc) - timedelta(days=VISIT_LOOKBACK_DAYS)
        end_utc = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
        return self._pages("/api/v1/visits", start_utc, end_utc,
                           lambda p: p if isinstance(p, list) else (p or {}).get("visits") or [])

    # ---- Dawarichs Aufenthalte und Fahrten (Rueckfall ohne Rohpunkte)
    def fetch(self, start: date, end: date) -> list[LocationEvent]:
        """Aufenthalte und Fahrten, die das lokale Fenster [start, end] beruehren."""
        window_start = timeutil.day_bounds(start)[0]
        window_end = timeutil.day_bounds(end)[1]
        start_utc = datetime.fromtimestamp(window_start / 1000, tz=timezone.utc)
        end_utc = datetime.fromtimestamp(window_end / 1000, tz=timezone.utc)

        events: list[LocationEvent] = []
        for raw in self.fetch_visits(window_start, window_end):
            ev = build_visit_event(raw)
            if ev is not None:
                events.append(ev)
        tracks = self._pages("/api/v1/tracks", start_utc, end_utc,
                             lambda p: (p or {}).get("features") or [])
        for raw in tracks:
            events.extend(build_track_events(raw))

        # Nur was das Fenster wirklich beruehrt - alles andere laege ausserhalb des ersetzten Bereichs.
        kept = [e for e in events if e.ts_end >= window_start and e.ts_start <= window_end]
        log.info("Dawarich: %d Aufenthalte, %d Fahrten fuer %s..%s",
                 sum(1 for e in kept if e.category == "visit"),
                 sum(1 for e in kept if e.category == "track"), start, end)
        return kept

    def test_connection(self) -> str:
        now = timeutil.now_ms()
        if self.points_available():
            points = self.fetch_points(now - 86_400_000, now)
            return (f"Verbindung erfolgreich - GPS-Rohpunkte freigegeben ({len(points)} Punkte in den letzten "
                    "24 Stunden). Zeitspur berechnet Aufenthalte und Fahrten selbst.")
        today = date.today()
        events = self.fetch(today - timedelta(days=7), today)
        visits = sum(1 for e in events if e.category == "visit")
        tracks = sum(1 for e in events if e.category == "track")
        return (f"Verbindung erfolgreich - letzte 7 Tage: {visits} Aufenthalte, {tracks} Fahrten. Hinweis: "
                "/api/v1/points ist nicht erreichbar (404). Für eine genaue, lückenlose Ortsspur diesen "
                "Endpunkt freigeben (z. B. im vorgeschalteten Proxy).")


# --------------------------------------------------------------------------- Abgleich

class Removed(Exception):
    """Das Plugin wurde waehrend des Abgleichs entfernt - nichts mehr schreiben."""


def sync(client: DawarichClient, storage: "Storage", start: date, end: date, *,
         now_ms: int | None = None, still_wanted=lambda: True) -> list[dict]:
    """Belege fuer [start, end]: aus Rohpunkten, wenn der Server sie liefert, sonst aus visits/tracks.

    Rohpunkte werden je Tag abgeholt und lokal (verschluesselt) gespeichert. Abgeschlossene Tage holt der
    naechste Abgleich nicht erneut - nur heute und Tage, die noch Nachzuegler bekommen koennen.
    """
    now_ms = timeutil.now_ms() if now_ms is None else now_ms
    window_start, window_end = timeutil.day_bounds(start)[0], timeutil.day_bounds(end)[1]
    if not client.points_available():
        log.info("Dawarich: /api/v1/points nicht freigegeben - nutze Aufenthalte und Fahrten von Dawarich")
        return [e.as_row() for e in client.fetch(start, end)]
    try:
        fetched = json.loads(storage.get_meta(POINTS_META_KEY) or "{}")
        fetched = fetched if isinstance(fetched, dict) else {}
    except ValueError:
        fetched = {}
    days = days_to_fetch(start, end, fetched, now_ms)
    total = 0
    for d in days:
        day_start, day_end = timeutil.day_bounds(d)
        points = client.fetch_points(day_start, day_end)
        if not still_wanted():   # waehrend des Abrufs entfernt: keine Bewegungsdaten mehr ablegen
            raise Removed("Plugin entfernt")
        storage.replace_location_points(SOURCE, day_start, day_end, [(p.ts, p.lat, p.lon, p.acc) for p in points])
        fetched[d.isoformat()] = now_ms
        total += len(points)
        # nach jedem Tag merken - bricht der Abgleich ab, muss nicht alles erneut geholt werden
        storage.set_meta(POINTS_META_KEY, json.dumps(fetched, sort_keys=True))
    oldest = (timeutil.local_date(now_ms) - timedelta(days=60)).isoformat()
    fetched = {k: v for k, v in fetched.items() if k >= oldest}
    storage.set_meta(POINTS_META_KEY, json.dumps(fetched, sort_keys=True))
    if days:
        log.info("Dawarich: %d Punkte fuer %d Tag(e) abgeholt", total, len(days))
    # Ersetzt werden alle Belege, die das Fenster beruehren - auch ein Aufenthalt, der drei Tage frueher begann
    # oder bis uebermorgen reicht. Damit er nicht gekappt wird, die Punkte ueber seine ganze Dauer laden.
    existing = storage.events_between(window_start, window_end, sources=[SOURCE])
    lo = min([window_start - location.LOOKBACK_MS] + [int(r["ts_start"]) for r in existing])
    hi = max([window_end] + [int(r["ts_end"]) + 1 for r in existing])
    rows = storage.location_points(SOURCE, lo, hi)
    points = [Point(r["ts"], r["lat"], r["lon"], r["accuracy"]) for r in rows]
    try:
        visits = client.fetch_visits(window_start, window_end)
    except DawarichError as e:
        log.warning("Dawarich: Aufenthalte (fuer die Namen) nicht abrufbar: %s", e)
        visits = []
    return events_from_points(points, visits, window_start, window_end)
