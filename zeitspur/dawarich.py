"""Standort-Historie aus einer privaten Dawarich-Instanz (schreibgeschuetzte HTTPS-Schnittstelle).

Es gibt genau zwei Endpunkte: /api/v1/visits (erkannte Aufenthalte) und /api/v1/tracks (Fahrten als
GeoJSON). Beides wird auf unser Ereignis-Schema abgebildet und landet zusammen mit Outlook-Terminen und
Teams-Anrufen in calendar_events (source='dawarich').

Sicherheit: Basis-Adresse und Token liegen per DPAPI geschuetzt in
%LOCALAPPDATA%\\Zeitspur\\dawarich_credentials.bin - nie im Klartext-config.yaml. Der Token geht
ausschliesslich in den Authorization-Header, nie in die URL (URLs landen in Proxy-Logs) und niemals in
unsere Protokolle. Unverschluesseltes http:// wird abgelehnt.

Netzwerkzugriff bewusst nur ueber die Standardbibliothek (urllib), wie bei den anderen Quellen.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import timeutil
from .config import data_dir

log = logging.getLogger(__name__)

SOURCE = "dawarich"
DAWARICH_ENTROPY = b"Zeitspur-Dawarich-credentials-v1"   # darf sich nie aendern, sonst sind gespeicherte Zugangsdaten unlesbar
CREDENTIALS_FILE = "dawarich_credentials.bin"
HTTP_TIMEOUT = 30
PER_PAGE = 500          # Obergrenze der Schnittstelle
MAX_PAGES = 10          # Sicherheitsnetz gegen Endlosschleifen
# Aufenthalte werden nach started_at gefiltert: einer, der vor dem Fenster begann und hineinreicht,
# fiele sonst weg. Deshalb grosszuegig frueher abfragen und selbst auf Ueberlappung filtern.
VISIT_LOOKBACK_DAYS = 7
DECLINED = "declined"
MAX_PATH_POINTS = 500   # Obergrenze je Fahrt, damit die Datenbank nicht mit Rohpunkten volllaeuft
PATH_EPSILON = 2e-5     # ~2 m; feiner braucht die Kartendarstellung nicht


class DawarichError(Exception):
    pass


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
        return f"{float(lat):.5f}, {float(lon):.5f}"
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


def _fmt_km(km: float) -> str:
    return f"{km:.1f}".replace(".", ",") + " km"


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



def _simplify(points: list, epsilon: float = PATH_EPSILON) -> list:
    """Douglas-Peucker auf [(lat, lon), ...]: haelt die Form der Strecke, spart aber Platz.

    Bewusst iterativ statt rekursiv - eine lange Fahrt hat schnell mehrere tausend Rohpunkte und
    wuerde die Rekursionsgrenze reissen.
    """
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        best_i, best_d = -1, 0.0
        for i in range(first + 1, last):
            px, py = points[i]
            if norm == 0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best_d:
                best_i, best_d = i, d
        if best_d > epsilon and best_i > 0:
            keep[best_i] = True
            stack.append((first, best_i))
            stack.append((best_i, last))
    return [pt for pt, k in zip(points, keep) if k]


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
    pts = _simplify(pts)
    if len(pts) > MAX_PATH_POINTS:  # gleichmaessig ausduennen, Anfang und Ende bleiben erhalten
        step = len(pts) / MAX_PATH_POINTS
        thinned = [pts[min(int(i * step), len(pts) - 1)] for i in range(MAX_PATH_POINTS)]
        thinned[-1] = pts[-1]
        pts = thinned
    return [[round(lat, 5), round(lon, 5)] for lat, lon in pts]


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
    if isinstance(line, list) and line:
        # GeoJSON ist [Laengengrad, Breitengrad] - genau umgekehrt zur ueblichen Schreibweise.
        try:
            first = _coords(line[0][1], line[0][0])
            last = _coords(line[-1][1], line[-1][0])
        except (IndexError, TypeError):
            first = last = None
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
        if einzeln and first:
            extra["from"] = first
        if einzeln and last:
            extra["to"] = last
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
        location = None
        if einzeln:
            location = f"{first} nach {last}" if first and last and first != last else (first or last)
        events.append(LocationEvent(
            ext_id=None if tid is None else (f"track-{tid}" if einzeln else f"track-{tid}-{nr}"),
            ts_start=int(von), ts_end=int(bis),
            subject=label + (f" - {_fmt_km(km)}" if km is not None else ""),
            location=location, organizer=None, attendees=None,
            category="track", extra=json.dumps(extra, ensure_ascii=False)))
    return events



# --------------------------------------------------------------------------- Bekannte Orte

DEFAULT_PLACE_RADIUS_M = 150.0


@dataclass
class KnownPlace:
    """Ein benannter Ort, damit aus Koordinaten ein Name wird ("Buero" statt "52.51627, 13.37770")."""

    name: str
    lat: float
    lon: float
    radius_m: float = DEFAULT_PLACE_RADIUS_M


def parse_known_place(entry: str) -> KnownPlace:
    """'Name;lat;lon' oder 'Name;lat;lon;radius_m' -> KnownPlace. Wirft ValueError bei Unsinn."""
    parts = [p.strip() for p in str(entry).split(";")]
    if len(parts) < 3:
        raise ValueError(f"{entry!r}: erwartet 'Name;Breite;Laenge' (optional ';Radius in Metern')")
    name = parts[0]
    if not name:
        raise ValueError(f"{entry!r}: der Name fehlt")
    try:
        lat, lon = float(parts[1].replace(",", ".")), float(parts[2].replace(",", "."))
        radius = float(parts[3].replace(",", ".")) if len(parts) > 3 and parts[3] else DEFAULT_PLACE_RADIUS_M
    except ValueError as e:
        raise ValueError(f"{entry!r}: Breite/Laenge/Radius muessen Zahlen sein") from e
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise ValueError(f"{entry!r}: Breite muss -90..90, Laenge -180..180 sein")
    if radius <= 0:
        raise ValueError(f"{entry!r}: der Radius muss groesser als 0 sein")
    return KnownPlace(name, lat, lon, radius)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Entfernung in Metern (Haversine) - kein Koordinatenvergleich, sonst waere der Radius breitenabhaengig."""
    import math

    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def match_place(lat, lon, places) -> "KnownPlace | None":
    """Naechstgelegener bekannter Ort innerhalb seines Radius - sonst None (dann bleiben die Koordinaten)."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    best, best_d = None, None
    for place in places or []:
        d = distance_m(lat, lon, place.lat, place.lon)
        if d <= place.radius_m and (best_d is None or d < best_d):
            best, best_d = place, d
    return best


def places_from_config(cfg) -> list:
    """Bekannte Orte aus der Konfiguration; der Kartenstartpunkt zaehlt automatisch als Ort."""
    places: list[KnownPlace] = []
    label = (getattr(cfg, "map_home_label", "") or "").strip()
    if label:
        places.append(KnownPlace(label, float(cfg.map_home_lat), float(cfg.map_home_lon)))
    for entry in getattr(cfg, "known_places", None) or []:
        try:
            places.append(parse_known_place(entry))
        except ValueError as e:
            log.warning("Bekannter Ort wird uebersprungen: %s", e)
    return places


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
    def __init__(self, credentials: dict):
        self.base_url = normalize_base_url(credentials["base_url"])
        self._token = credentials["token"]

    @classmethod
    def from_stored(cls, path: Path | None = None) -> "DawarichClient | None":
        creds = load_credentials(path)
        return cls(creds) if creds else None

    def _get(self, path: str, params: dict):
        url = self.base_url + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {self._token}",  # nur im Header, nie in der URL
            "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise DawarichError(_http_message(e.code)) from e
        except urllib.error.URLError as e:
            raise DawarichError(f"Standort-Dienst nicht erreichbar: {e.reason}") from e
        except ValueError as e:
            raise DawarichError(f"Unerwartete Antwort: {e}") from e

    def _pages(self, path: str, start_utc: datetime, end_utc: datetime, extract) -> list:
        """Seitenweise abfragen. Die Schnittstelle erlaubt 60 Anfragen/Minute - deshalb grosse
        Zeitraeume am Stueck und hoechstens MAX_PAGES Seiten."""
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

    def fetch(self, start: date, end: date) -> list[LocationEvent]:
        """Aufenthalte und Fahrten, die das lokale Fenster [start, end] beruehren."""
        window_start = timeutil.day_bounds(start)[0]
        window_end = timeutil.day_bounds(end)[1]
        start_utc = datetime.fromtimestamp(window_start / 1000, tz=timezone.utc)
        end_utc = datetime.fromtimestamp(window_end / 1000, tz=timezone.utc)

        events: list[LocationEvent] = []
        visits = self._pages("/api/v1/visits", start_utc - timedelta(days=VISIT_LOOKBACK_DAYS), end_utc,
                             lambda p: p if isinstance(p, list) else (p or {}).get("visits") or [])
        for raw in visits:
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
        today = date.today()
        events = self.fetch(today - timedelta(days=7), today)
        visits = sum(1 for e in events if e.category == "visit")
        tracks = sum(1 for e in events if e.category == "track")
        return f"Verbindung erfolgreich - letzte 7 Tage: {visits} Aufenthalte, {tracks} Fahrten."
