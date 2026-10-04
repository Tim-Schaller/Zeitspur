"""Standort-Spur: aus den Ortsbelegen aller Quellen eine lueckenlose Tagesleiste "Ort -> Fahrt -> Ort".

Ziel ist eine Leiste, die man ohne Nachdenken liest: "08:20-16:40 Büro, 25 min Fahrt nach Zuhause,
17:05-22:00 Zuhause". Die Quellen liefern dafuer nur Belege, und jede hat ihre eigenen Schwaechen:

- GPS vom Handy (Plugin Dawarich): beste Quelle fuer Fahrten und dafuer, wo der Mensch ist. Hat Luecken,
  wenn das Handy nichts sendet - beim Stillstehen ist das sogar normal.
- WLAN des PCs: sehr verlaesslich, WO der PC ist - am staerksten, solange er wach ist (im Standby kann er
  im Buero stehen, waehrend der Mensch laengst zu Hause ist).
- Windows-Standort des PCs: Koordinaten statt Netznamen, sonst wie WLAN.

Die Zusammenfuehrung malt die Belege nach Verlaesslichkeit auf eine Minutenleiste (GPS-Aufenthalte vor
GPS-Fahrten vor PC-Belegen), fasst gleiche Orte zusammen und schliesst Luecken nach festen Regeln - etwa
"wer zwischen zwei Belegen am selben Ort keine Fahrt hatte, war die ganze Zeit dort". Was nur erschlossen
ist, bleibt als solches markiert: Die Leiste ist lueckenlos, aber ehrlich.

Reine Logik ohne Netz und ohne Datenbank - alles hier ist direkt testbar.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import timeutil
from .wifi import NO_PLACE as IGNORE_PLACE
from .wifi import parse_places as parse_wifi_places
from .wifi import ssid_key as norm_ssid

log = logging.getLogger(__name__)

# Quellen, deren Ereignisse Ortsbelege sind. Im Zeitstrahl und fuer Claude ersetzt die Standort-Spur sie.
EVIDENCE_SOURCES = ("dawarich", "windows_location", "wifi")
PC_TIMES_SOURCE = "pc_times"
# Benannte Aufenthalte aus diesem Umkreis (Tage) geben Koordinaten ihren Namen - auch wenn der Name am
# angezeigten Tag selbst nirgends vorkommt ("Zuhause" kennt Dawarich vielleicht nur von gestern).
ANCHOR_SOURCES = ("dawarich",)
ANCHOR_CATEGORIES = ("visit", "named_place")   # named_place: Dawarichs benannte Orte, nur zum Benennen
ANCHOR_RANGE_MS = 14 * 86_400_000

SLOT_MS = 60_000                  # Aufloesung der Leiste: eine Minute
LOOKBACK_MS = 36 * 3_600_000      # Belege vor dem Tag, damit klar ist, wo der Tag beginnt
LOOKAHEAD_MS = 36 * 3_600_000     # und danach, damit klar ist, wo er endet

DEFAULT_PLACE_RADIUS_M = 150.0
SAME_PLACE_M = 250.0              # zwei Belege naeher beieinander gelten als derselbe Ort
TRIP_END_M = 500.0                # Start/Ziel einer Fahrt passt zu einem Ort (erste GPS-Punkte liegen oft daneben)
STAY_RADIUS_M = 120.0             # GPS-Aufenthalt: alle Punkte in diesem Umkreis ...
MIN_STAY_MS = 5 * 60_000          # ... und mindestens so lange (darunter: Ampel, Tankstelle)
GPS_MAX_POINT_GAP_MS = 3 * 3_600_000   # laengere Funkstille innerhalb eines Aufenthalts gilt als erschlossen
MIN_TRIP_M = 300.0                # darunter ist es GPS-Zittern, keine Fahrt
TRIP_EDGE_GAP_MS = 10 * 60_000    # laengere Funkstille am Anfang/Ende einer Fahrt: Abfahrt bzw. Ankunft unbekannt
SILENCE_MS = 10 * 60_000          # so lange ohne Punkt mitten zwischen zwei Aufenthalten: zwei getrennte Fahrten
SILENT_START_M = 1500.0           # erster Punkt nach der Stille so nah am Aufenthalt davor: dort losgefahren
SPEED_WINDOW_S = 30               # Geschwindigkeit ueber mindestens so lange Abschnitte
MAX_ACCURACY_M = 200.0            # ungenauere Punkte (Funkzelle) werden verworfen
SPIKE_M = 400.0                   # Ausreisser: weit weg von beiden Nachbarn, die selbst beieinander liegen

INFER_TRIP_MAX_MS = 2 * 3_600_000      # Luecke zwischen zwei verschiedenen Orten: bis dahin "Fahrt"
TRIP_GAP_MAX_MS = 30 * 60_000          # Luecke zwischen zwei Fahrten mit unbekanntem Halt
EXTEND_MAX_MS = 16 * 3_600_000         # so lange gilt "noch dort" nach dem letzten Beleg ueberhaupt
SAME_PLACE_MAX_MS = 72 * 3_600_000     # derselbe Ort davor und danach: so lange darf die Luecke hoechstens sein
MIN_STOP_IN_TRIP_MS = 3 * 60_000       # kuerzere Halte zwischen zwei Fahrten gehoeren zur Fahrt
NOISE_TRIP_MS = 10 * 60_000            # kuerzere "Fahrt" vom Ort zum selben Ort: Zittern, Parkplatz
ALIAS_MIN_OVERLAP_MS = 30 * 60_000     # so lange muessen ein benannter und ein unbenannter Beleg zusammenfallen

# Verlaesslichkeit der Belege: hoeher gewinnt, wenn sich Belege widersprechen. GPS-Aufenthalte stehen vor
# GPS-Fahrten: Aus Rohpunkten berechnet ueberschneiden sie sich nie; Dawarichs eigene "Fahrten" umfassen
# dagegen oft Halte (Einkauf) oder ganze Arbeitstage, die als Aufenthalt erkannt sind.
PRIO_GPS_STAY = 50
PRIO_GPS_TRIP = 45
PRIO_WIFI_PLACE = 30
PRIO_PC = 25
PRIO_WIFI = 20
PRIO_WIFI_STANDBY = 15

MODE_LABELS = {"car": "Autofahrt", "bike": "Radfahrt", "walk": "Fußweg", "train": "Zugfahrt",
               "flight": "Flug", "boat": "Bootsfahrt"}
# Dawarichs eigene Bezeichnungen (dominant_mode) -> unsere
_MODE_ALIASES = {"driving": "car", "car": "car", "cycling": "bike", "bike": "bike", "walking": "walk",
                 "running": "walk", "train": "train", "flight": "flight", "boat": "boat",
                 "public_transport": None}


# =========================================================================== Geometrie

def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Entfernung in Metern (Haversine) - kein Koordinatenvergleich, sonst waere der Radius breitenabhaengig."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _near(a: tuple[float, float] | None, b: tuple[float, float] | None, limit: float = SAME_PLACE_M) -> bool:
    return a is not None and b is not None and distance_m(a[0], a[1], b[0], b[1]) <= limit


def fmt_coords(lat: float, lon: float) -> str:
    return f"{lat:.5f}, {lon:.5f}"


def fmt_km(km: float) -> str:
    return f"{km:.1f}".replace(".", ",") + " km"


# =========================================================================== Bekannte Orte

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


def format_known_place(place: KnownPlace) -> str:
    """Gegenstueck zu parse_known_place - so steht der Ort in config.yaml."""
    radius = int(place.radius_m) if float(place.radius_m).is_integer() else place.radius_m
    return f"{place.name};{place.lat:.6f};{place.lon:.6f};{radius}"


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


# Rechtsformen am Namensende zaehlen nicht: "Beispiel GmbH" (Karte) und "Beispiel" (WLAN) sind derselbe Ort.
_LEGAL_FORMS = {"gmbh", "mbh", "ag", "kg", "ug", "ohg", "gbr", "se", "co", "ev", "inc", "ltd", "llc"}


def norm_name(name: str | None) -> str:
    """Vergleichsform eines Ortsnamens: Kleinschreibung, nur Wortzeichen, ohne Rechtsform am Ende."""
    words = re.findall(r"\w+", (name or "").casefold())
    if len(words) > 2 and words[-2:] == ["e", "v"]:
        words = words[:-2]
    while len(words) > 1 and words[-1] in _LEGAL_FORMS:
        words.pop()
    return " ".join(words)


def place_by_name(name: str | None, places) -> "KnownPlace | None":
    key = norm_name(name)
    if not key:
        return None
    exact = (name or "").strip().casefold()
    matches = [p for p in places or [] if norm_name(p.name) == key]
    return next((p for p in matches if p.name.strip().casefold() == exact), matches[0] if matches else None)


def places_from_config(cfg) -> list:
    """Bekannte Orte aus der Konfiguration; der benannte Kartenstartpunkt zaehlt automatisch als Ort."""
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


def wifi_places_from_config(cfg) -> dict[str, str]:
    """WLAN-Zuordnungen des WLAN-Plugins als {Vergleichsform des Netznamens: Ort}."""
    lines = (((getattr(cfg, "plugin_settings", None) or {}).get("wifi") or {}).get("places")) or []
    try:
        return parse_wifi_places([str(x) for x in lines])
    except ValueError as e:
        log.warning("WLAN-Zuordnung unlesbar: %s", e)
        return {}


_REGIONS = {"bavaria", "bayern", "baden-württemberg", "baden-wuerttemberg", "berlin", "brandenburg", "bremen",
            "hamburg", "hesse", "hessen", "lower saxony", "niedersachsen", "mecklenburg-vorpommern",
            "mecklenburg-western pomerania", "north rhine-westphalia", "nordrhein-westfalen", "rhineland-palatinate",
            "rheinland-pfalz", "saarland", "saxony", "sachsen", "saxony-anhalt", "sachsen-anhalt",
            "schleswig-holstein", "thuringia", "thüringen", "germany", "deutschland", "austria", "österreich",
            "switzerland", "schweiz"}


def short_address(text: str | None) -> str:
    """'Supermarkt, Musterweg, 2, Beispielstadt, Bavaria' -> 'Supermarkt, Musterweg 2, Beispielstadt'."""
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    while len(parts) > 1 and parts[-1].casefold() in _REGIONS:
        parts.pop()
    out: list[str] = []
    for p in parts:
        if out and re.fullmatch(r"\d+\s*[a-zA-Z]?", p):
            out[-1] = f"{out[-1]} {p}"   # Hausnummer gehoert zur Strasse
        else:
            out.append(p)
    return ", ".join(out)


# =========================================================================== GPS-Punkte -> Aufenthalte/Fahrten

@dataclass
class Point:
    ts: int                     # ms
    lat: float
    lon: float
    acc: float | None = None    # Genauigkeit in Metern, falls bekannt


@dataclass
class StayCandidate:
    start: int
    end: int
    lat: float
    lon: float
    points: int
    i0: int = 0                 # erster und letzter Punkt (Index in der bereinigten Punktliste)
    i1: int = 0
    parts: list = field(default_factory=list)   # [(start, end)] mit Daten; dazwischen lange Funkstille


@dataclass
class TripCandidate:
    start: int
    end: int
    distance_m: float
    path: list
    mode: str | None
    from_pt: tuple[float, float]
    to_pt: tuple[float, float]
    max_kmh: float | None = None


def clean_points(points: Iterable[Point], max_accuracy_m: float = MAX_ACCURACY_M) -> list[Point]:
    """Sortiert, entfernt Doppelte, ungenaue Punkte und einzelne Ausreisser."""
    by_ts: dict[int, Point] = {}
    for p in points:
        if p.acc is not None and p.acc > max_accuracy_m:
            continue
        if not (-90 <= p.lat <= 90 and -180 <= p.lon <= 180) or (p.lat == 0 and p.lon == 0):
            continue
        by_ts[int(p.ts)] = p
    pts = [by_ts[t] for t in sorted(by_ts)]
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, p, b = out[-1], pts[i], pts[i + 1]
        far_a = distance_m(a.lat, a.lon, p.lat, p.lon) > SPIKE_M
        far_b = distance_m(p.lat, p.lon, b.lat, b.lon) > SPIKE_M
        if far_a and far_b and distance_m(a.lat, a.lon, b.lat, b.lon) < SPIKE_M / 2:
            continue   # hin und sofort zurueck: ein Fehlpunkt, keine Bewegung
        out.append(p)
    out.append(pts[-1])
    return out


def detect_stays(points: list[Point], *, radius_m: float = STAY_RADIUS_M,
                 min_ms: int = MIN_STAY_MS) -> list[StayCandidate]:
    """Aufenthalte: aufeinanderfolgende Punkte im Umkreis radius_m um ihren Schwerpunkt, mindestens min_ms lang.

    Duenne Daten sind kein Problem: Ein Punkt um 08:20 und der naechste um 11:30 am selben Ort ergeben einen
    Aufenthalt von 08:20 bis 11:30 - das Handy meldet sich beim Stillstehen eben selten. Wie lange es
    zwischendurch still war, haelt split_parts() fest.
    """
    stays: list[StayCandidate] = []
    i, n = 0, len(points)
    while i < n:
        lat_sum, lon_sum, k = points[i].lat, points[i].lon, 1
        j = i + 1
        while j < n:
            if distance_m(lat_sum / k, lon_sum / k, points[j].lat, points[j].lon) > radius_m:
                break
            lat_sum += points[j].lat
            lon_sum += points[j].lon
            k += 1
            j += 1
        if points[j - 1].ts - points[i].ts >= min_ms:
            stays.append(StayCandidate(points[i].ts, points[j - 1].ts, lat_sum / k, lon_sum / k, k, i, j - 1))
            i = j
        else:
            i += 1
    return stays


def _absorb(a: StayCandidate, b: StayCandidate) -> None:
    """b in a aufgehen lassen (b folgt zeitlich auf a)."""
    w1, w2 = a.points, b.points
    a.lat = (a.lat * w1 + b.lat * w2) / (w1 + w2)
    a.lon = (a.lon * w1 + b.lon * w2) / (w1 + w2)
    a.end, a.points, a.i1 = b.end, w1 + w2, b.i1


def split_parts(points: list[Point], stay: StayCandidate, max_gap_ms: int) -> list[tuple[int, int]]:
    """Zeitabschnitte eines Aufenthalts mit Daten - getrennt an Funkstillen ueber max_gap_ms.

    Die Funkstille selbst ist nicht belegt; die Zusammenfuehrung schliesst sie als "erschlossen", wenn
    davor und danach derselbe Ort steht.
    """
    parts: list[tuple[int, int]] = []
    start = points[stay.i0].ts
    for a, b in zip(points[stay.i0:stay.i1], points[stay.i0 + 1:stay.i1 + 1]):
        if b.ts - a.ts > max_gap_ms:
            parts.append((start, a.ts))
            start = b.ts
    parts.append((start, points[stay.i1].ts))
    return parts


def _path_length_m(points: list[Point]) -> float:
    return sum(distance_m(a.lat, a.lon, b.lat, b.lon) for a, b in zip(points, points[1:]))


def _max_deviation_m(points: list[Point], lat: float, lon: float) -> float:
    return max((distance_m(lat, lon, p.lat, p.lon) for p in points), default=0.0)


def classify_mode(points: list[Point]) -> tuple[str | None, float | None]:
    """Fortbewegungsart grob aus den Geschwindigkeiten: (Art oder None, Hoechstgeschwindigkeit km/h).

    Nur was eindeutig ist: Ueber ~40 km/h faehrt man nicht Rad, unter ~8 km/h faehrt man nicht Auto.
    Dazwischen (Rad, Stadtverkehr, Stau) bleibt es eine "Fahrt" - lieber allgemein als falsch.
    """
    # Ueber Abschnitte von mindestens SPEED_WINDOW_S: Bei einem Punkt je Sekunde waeren Paare zu kurz (GPS-Zittern),
    # und nur Paare mit grossem Abstand zu nehmen hiesse, fast nur Ampelstopps zu messen.
    speeds = []
    i, run = 0, 0.0
    for j in range(1, len(points)):
        run += distance_m(points[j - 1].lat, points[j - 1].lon, points[j].lat, points[j].lon)
        dt = (points[j].ts - points[i].ts) / 1000
        if dt >= SPEED_WINDOW_S:
            speeds.append(run / dt * 3.6)
            i, run = j, 0.0
    if len(speeds) < 2:
        return None, None
    speeds.sort()
    v90 = speeds[min(len(speeds) - 1, int(len(speeds) * 0.9))]
    if v90 >= 300:
        return "flight", v90
    if v90 >= 40:
        return "car", v90
    if v90 < 8:
        return "walk", v90
    return None, v90


def simplify_path(points: list[tuple[float, float]], epsilon: float = 2e-5, max_points: int = 500) -> list:
    """Douglas-Peucker auf [(lat, lon), ...]: haelt die Form der Strecke, spart aber Platz.

    Bewusst iterativ statt rekursiv - eine lange Fahrt hat schnell mehrere tausend Rohpunkte und
    wuerde die Rekursionsgrenze reissen.
    """
    if len(points) < 3:
        pts = list(points)
    else:
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
        pts = [pt for pt, k in zip(points, keep) if k]
    if len(pts) > max_points:   # gleichmaessig ausduennen, Anfang und Ende bleiben erhalten
        step = len(pts) / max_points
        thinned = [pts[min(int(i * step), len(pts) - 1)] for i in range(max_points)]
        thinned[-1] = pts[-1]
        pts = thinned
    return [[round(lat, 5), round(lon, 5)] for lat, lon in pts]


def _trip(points: list[Point]) -> TripCandidate:
    """Fahrt aus den Punkten zwischen zwei Aufenthalten (erster und letzter Punkt gehoeren noch zu ihnen).

    Schweigt das Handy nach dem letzten Punkt am Ort lange (die Nacht ueber), beginnt die Fahrt nicht dort,
    sondern erst mit dem naechsten Punkt - wann genau abgefahren wurde, ist unbekannt; die Zusammenfuehrung
    schliesst die Stille als "noch dort". Ebenso am Ende.
    """
    mode, vmax = classify_mode(points)
    start, end = points[0].ts, points[-1].ts
    if len(points) > 2 and points[1].ts - start > TRIP_EDGE_GAP_MS:
        start = points[1].ts
    if len(points) > 2 and end - points[-2].ts > TRIP_EDGE_GAP_MS:
        end = max(start, points[-2].ts)
    return TripCandidate(
        start=start, end=end, distance_m=_path_length_m(points),
        path=simplify_path([(p.lat, p.lon) for p in points]), mode=mode,
        from_pt=(points[0].lat, points[0].lon), to_pt=(points[-1].lat, points[-1].lon), max_kmh=vmax)


def _split_at_silence(points: list[Point]) -> list[list[Point]]:
    """Punktfolge an Funkstillen ueber SILENCE_MS teilen."""
    pieces: list[list[Point]] = []
    for p in points:
        if pieces and p.ts - pieces[-1][-1].ts <= SILENCE_MS:
            pieces[-1].append(p)
        else:
            pieces.append([p])
    return pieces


def segment_points(points: Iterable[Point], *,
                   max_gap_ms: int = GPS_MAX_POINT_GAP_MS) -> tuple[list[StayCandidate], list[TripCandidate]]:
    """GPS-Punkte -> Aufenthalte und die Fahrten dazwischen.

    Eine Fahrt beginnt mit dem letzten Punkt eines Aufenthalts und endet mit dem ersten des naechsten. Zwei
    Aufenthalte verschmelzen nur, wenn dazwischen niemand den Ort verlassen hat (GPS-Zittern im Haus) - wer
    eine Runde faehrt und zurueckkommt, war unterwegs. Am Ende der Daten gilt: Wer gerade angekommen ist,
    steht schon am neuen Ort (auch vor Ablauf von fuenf Minuten); wer noch faehrt, ist unterwegs.
    """
    pts = clean_points(points)
    if not pts:
        return [], []
    merged: list[StayCandidate] = []
    for s in detect_stays(pts):
        if merged:
            prev = merged[-1]
            leg = pts[prev.i1:s.i0 + 1]
            if (distance_m(prev.lat, prev.lon, s.lat, s.lon) <= SAME_PLACE_M
                    and _max_deviation_m(leg, prev.lat, prev.lon) <= SAME_PLACE_M):
                _absorb(prev, s)
                continue
        merged.append(s)
    stays = merged
    trips: list[TripCandidate] = []

    def moves(leg: list[Point], *, from_stay: bool = True, to_stay: bool = True) -> list[TripCandidate]:
        """Fahrten in einem Abschnitt ohne Aufenthalt - getrennt an Funkstillen: Viele Handys zeichnen nur
        waehrend der Bewegung auf. Kommt es an und schweigt dann einen Tag, ist das Ankunft, Stille und
        spaeter eine neue Fahrt - keine Fahrt ueber 30 Stunden.

        Das Handy beginnt oft erst unterwegs zu senden: Liegt der erste Punkt nach der Stille nahe am
        Aufenthalt davor, ist dort der Start (Zeit bleibt die des ersten Punkts). Ebenso am Ziel."""
        pieces = _split_at_silence(leg)
        out = []
        for k, piece in enumerate(pieces):
            if len(piece) < 2 or _path_length_m(piece) < MIN_TRIP_M:
                continue
            tr = _trip(piece)
            if from_stay and k == 1 and len(pieces[0]) == 1 and \
                    distance_m(leg[0].lat, leg[0].lon, piece[0].lat, piece[0].lon) <= SILENT_START_M:
                tr.from_pt = (leg[0].lat, leg[0].lon)
            if to_stay and k == len(pieces) - 2 and len(pieces[-1]) == 1 and \
                    distance_m(leg[-1].lat, leg[-1].lon, piece[-1].lat, piece[-1].lon) <= SILENT_START_M:
                tr.to_pt = (leg[-1].lat, leg[-1].lon)
            out.append(tr)
        return out

    for a, b in zip(stays, stays[1:]):
        leg = pts[a.i1:b.i0 + 1]
        found = moves(leg)
        if not found and len(leg) >= 2 and distance_m(a.lat, a.lon, b.lat, b.lon) > SAME_PLACE_M:
            found = [_trip(leg)]   # kurzer Weg zwischen zwei nahen, aber verschiedenen Orten
        trips.extend(found)
    # Vor dem ersten Aufenthalt: beginnen die Daten mitten in einer Fahrt?
    if stays and stays[0].i0 > 0:
        trips[:0] = moves(pts[:stays[0].i0 + 1], from_stay=False)
    # Nach dem letzten Aufenthalt (oder ganz ohne): noch unterwegs oder schon angekommen?
    i_tail = stays[-1].i1 if stays else 0
    *earlier, tail = _split_at_silence(pts[i_tail:]) or [[]]
    if earlier:   # Fahrten vor der letzten Stille; die letzte Strecke (tail) behandelt die Ankunft unten
        trips.extend(moves([p for piece in earlier for p in piece], from_stay=bool(stays), to_stay=False))
    i_tail = len(pts) - len(tail)
    if len(tail) >= 2:
        last = tail[-1]
        m = len(tail) - 1
        while m > 0 and distance_m(tail[m - 1].lat, tail[m - 1].lon, last.lat, last.lon) <= STAY_RADIUS_M:
            m -= 1
        moving = tail[:m + 1]
        if len(moving) >= 2 and _path_length_m(moving) >= MIN_TRIP_M:
            tr = _trip(moving)
            before = earlier[-1][-1] if earlier else None   # letzter Punkt vor der Stille
            if before is not None and distance_m(before.lat, before.lon, tail[0].lat, tail[0].lon) <= SILENT_START_M:
                tr.from_pt = (before.lat, before.lon)
            trips.append(tr)
            arrived = tail[m:]
            stays.append(StayCandidate(arrived[0].ts, arrived[-1].ts,
                                       sum(p.lat for p in arrived) / len(arrived),
                                       sum(p.lon for p in arrived) / len(arrived),
                                       len(arrived), i_tail + m, len(pts) - 1))
        elif not earlier and stays and _max_deviation_m(tail, stays[-1].lat, stays[-1].lon) <= SAME_PLACE_M:
            stays[-1].end, stays[-1].i1 = last.ts, len(pts) - 1   # noch immer dort
    for s in stays:
        s.parts = split_parts(pts, s, max_gap_ms)
    return stays, trips


# =========================================================================== Belege

@dataclass
class Evidence:
    """Ein Ortsbeleg aus einer Quelle: Aufenthalt ("stay") oder Fahrt ("trip")."""

    kind: str
    start: int
    end: int
    prio: int
    source: str                         # Anzeige, z. B. "GPS (Dawarich)" oder "WLAN „Firma“"
    name: str | None = None             # Ortsname aus der Quelle (WLAN-Zuordnung, Dawarich)
    lat: float | None = None
    lon: float | None = None
    ssid: str | None = None
    address: str | None = None          # Adresse aus der Quelle, falls kein Name
    from_pt: tuple[float, float] | None = None
    to_pt: tuple[float, float] | None = None
    distance_km: float | None = None
    mode: str | None = None
    path: list | None = None
    # nach der Aufloesung
    key: str | None = None

    @property
    def coords(self) -> tuple[float, float] | None:
        return (self.lat, self.lon) if self.lat is not None and self.lon is not None else None


def _extra(row: dict) -> dict:
    try:
        value = json.loads(row.get("extra") or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _float(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pt(value) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        lat, lon = _float(value[0]), _float(value[1])
        if lat is not None and lon is not None:
            return lat, lon
    if isinstance(value, str) and "," in value:
        a, _, b = value.partition(",")
        return _pt([a, b])
    return None


def _intersect(start: int, end: int, periods: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for a, b in periods:
        s, e = max(start, a), min(end, b)
        if e > s:
            out.append((s, e))
    return out


def _subtract(start: int, end: int, periods: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out, cur = [], start
    for a, b in periods:
        if b <= cur or a >= end:
            continue
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    if cur < end:
        out.append((cur, end))
    return out


def evidence_from_events(rows: Iterable[dict], *, wifi_places: dict[str, str] | None = None) -> list[Evidence]:
    """calendar_events-Zeilen der Ortsquellen -> Belege.

    WLAN zaehlt voll, solange der PC wach war. Liefert das Plugin "PC-Zeiten" Daten, wird der Rest (PC im
    Standby, aber noch im Netz) zum schwachen Beleg: Er sagt, wo der PC stand - der Mensch kann woanders sein.
    """
    rows = list(rows)
    wifi_places = wifi_places or {}
    pc_on = sorted((int(r["ts_start"]), int(r["ts_end"])) for r in rows
                   if r.get("source") == PC_TIMES_SOURCE and r.get("category") == "pc_on")
    out: list[Evidence] = []
    for r in rows:
        source, cat = r.get("source"), r.get("category")
        start, end = int(r["ts_start"]), int(r["ts_end"])
        if end < start:
            continue
        x = _extra(r)
        if source == "dawarich" and cat == "visit":
            name = (x.get("name") or "").strip() or None
            if name is None and not str(r.get("subject") or "").startswith("Aufenthalt"):
                name = (r.get("subject") or "").strip() or None   # Zeilen aus aelteren Versionen
            out.append(Evidence("stay", start, end, PRIO_GPS_STAY, "GPS (Dawarich)",
                                lat=_float(x.get("latitude")), lon=_float(x.get("longitude")),
                                address=short_address(name) if name else None))
        elif source == "dawarich" and cat == "track":
            path = x.get("path") if isinstance(x.get("path"), list) and not x.get("path_covers_whole_track") else None
            from_pt = _pt(x.get("from")) or (_pt(path[0]) if path else None)
            to_pt = _pt(x.get("to")) or (_pt(path[-1]) if path else None)
            mode = x.get("mode")
            out.append(Evidence("trip", start, end, PRIO_GPS_TRIP, "GPS (Dawarich)",
                                from_pt=from_pt, to_pt=to_pt, distance_km=_float(x.get("distance_km")),
                                mode=_MODE_ALIASES.get(str(mode).lower(), mode) if mode else None, path=path))
        elif source == "windows_location" and cat == "visit":
            out.append(Evidence("stay", start, end, PRIO_PC, "Windows-Standort",
                                lat=_float(x.get("latitude")), lon=_float(x.get("longitude"))))
        elif source == "wifi" and cat == "wifi":
            ssid = (x.get("ssid") or "").strip() or None
            name = wifi_places.get(norm_ssid(ssid)) if ssid else None
            if name is None and not wifi_places:
                name = x.get("place") or None   # Zuordnung zum Zeitpunkt des Abgleichs
            if name == IGNORE_PLACE:
                continue
            label = f"WLAN „{ssid}“" if ssid else "WLAN"
            awake = _intersect(start, end, pc_on) if pc_on else [(start, end)]
            asleep = _subtract(start, end, pc_on) if pc_on else []
            for s, e in awake:
                out.append(Evidence("stay", s, e, PRIO_WIFI_PLACE if name else PRIO_WIFI, label,
                                    name=name, ssid=ssid))
            for s, e in asleep:
                out.append(Evidence("stay", s, e, PRIO_WIFI_STANDBY, label + " (PC im Standby)",
                                    name=name, ssid=ssid))
    return out


# =========================================================================== Orte aufloesen

@dataclass
class _PlaceInfo:
    key: str
    name: str | None
    lat: float | None
    lon: float | None
    known: bool
    rank: int        # kleiner = besserer Name: 0 bekannt, 1 WLAN, 2 Dawarich, 3 Netzname, 4 Koordinaten
    ssid: str | None = None


class _Resolver:
    """Gibt jedem Aufenthalt einen Ortsschluessel - gleiche Orte bekommen denselben, egal aus welcher Quelle."""

    def __init__(self, places: list[KnownPlace]):
        self.places = places
        # Schluessel je bekanntem Ort nach Position: zwei Orte gleichen Namens (zwei "Buero") bleiben zwei Orte
        self._kp_keys = {id(kp): f"kp:{i}" for i, kp in enumerate(places)}
        self.info: dict[str, _PlaceInfo] = {}
        self.parent: dict[str, str] = {}
        self._clusters: list[tuple[float, float, str]] = []   # Anker fuer Koordinaten ohne Namen

    def _add(self, info: _PlaceInfo) -> str:
        cur = self.info.get(info.key)
        if cur is None:
            self.info[info.key] = info
        else:
            if info.rank < cur.rank:   # besserer Name - Koordinaten und Netz des alten Eintrags behalten
                info.lat, info.lon = (info.lat, info.lon) if info.lat is not None else (cur.lat, cur.lon)
                info.ssid = info.ssid or cur.ssid
                self.info[info.key] = cur = info
            if cur.lat is None and info.lat is not None:
                cur.lat, cur.lon = info.lat, info.lon
            cur.ssid = cur.ssid or info.ssid
        self.parent.setdefault(info.key, info.key)
        return info.key

    def _known(self, kp: KnownPlace) -> str:
        key = self._kp_keys.get(id(kp)) or f"kp:{norm_name(kp.name)}:{kp.lat:.5f},{kp.lon:.5f}"
        return self._add(_PlaceInfo(key, kp.name, kp.lat, kp.lon, True, 0))

    def key_for_named(self, ev: Evidence) -> str | None:
        """Erster Durchgang: alles, was einen Namen hat oder an einem bekannten Ort liegt."""
        kp = match_place(ev.lat, ev.lon, self.places) if ev.coords else None
        kp = kp or place_by_name(ev.name, self.places) or place_by_name(ev.address, self.places)
        if kp is not None:
            key = self._known(kp)
        elif ev.name:
            key = self._add(_PlaceInfo("n:" + norm_name(ev.name), ev.name, ev.lat, ev.lon, False, 1, ssid=ev.ssid))
        elif ev.address:
            key = self._add(_PlaceInfo("n:" + norm_name(ev.address), ev.address, ev.lat, ev.lon, False, 2))
        else:
            return None
        if ev.coords:
            self._clusters.append((ev.lat, ev.lon, key))
        return key

    def key_for_coords(self, lat: float, lon: float) -> str:
        """Koordinaten ohne Namen: bekannter Ort, sonst ein benachbarter Beleg, sonst ein neuer namenloser Ort."""
        kp = match_place(lat, lon, self.places)
        if kp is not None:
            return self._known(kp)
        best = None
        for c_lat, c_lon, key in self._clusters:
            d = distance_m(lat, lon, c_lat, c_lon)
            if d <= SAME_PLACE_M and (best is None or d < best[0]):
                best = (d, key)
        if best is not None:
            return best[1]
        key = self._add(_PlaceInfo(f"g:{len(self._clusters)}", None, lat, lon, False, 4))
        self._clusters.append((lat, lon, key))
        return key

    def key_for_ssid(self, ssid: str) -> str:
        return self._add(_PlaceInfo("s:" + norm_ssid(ssid), None, None, None, False, 3, ssid=ssid))

    def find(self, key: str) -> str:
        while self.parent.get(key, key) != key:
            self.parent[key] = self.parent.get(self.parent[key], self.parent[key])
            key = self.parent[key]
        return key

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Wurzel ist der besser benannte Ort; ihm fehlende Koordinaten uebernimmt er vom anderen
        if (self.info[rb].rank, rb) < (self.info[ra].rank, ra):
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.info[ra].lat is None and self.info[rb].lat is not None:
            self.info[ra].lat, self.info[ra].lon = self.info[rb].lat, self.info[rb].lon
        if self.info[ra].ssid is None:
            self.info[ra].ssid = self.info[rb].ssid

    def place(self, key: str) -> _PlaceInfo:
        return self.info[self.find(key)]

    def weak(self, key: str) -> bool:
        return self.info[key].rank >= 3


def resolve(evidence: list[Evidence], places: list[KnownPlace], anchors: Iterable[Evidence] = ()) -> _Resolver:
    res = _Resolver(places)
    for ev in anchors:   # nur zum Benennen von Koordinaten, nicht Teil der Leiste
        if ev.kind == "stay" and ev.coords and (ev.name or ev.address):
            res.key_for_named(ev)
    stays = [e for e in evidence if e.kind == "stay"]
    for ev in stays:
        ev.key = res.key_for_named(ev)
    for ev in stays:
        if ev.key is None and ev.coords:
            ev.key = res.key_for_coords(ev.lat, ev.lon)
        elif ev.key is None and ev.ssid:
            ev.key = res.key_for_ssid(ev.ssid)
        elif ev.key is None:
            ev.key = res._add(_PlaceInfo("x:?", None, None, None, False, 5))
    _learn_aliases(res, stays)
    return res


def _learn_aliases(res: "_Resolver", stays: list[Evidence]) -> None:
    """Ein namenloser Ort, der lange mit einem benannten zusammenfaellt, IST dieser Ort: Das Handy steht an
    unbekannten Koordinaten, waehrend der PC im WLAN "Zuhause" haengt -> die Koordinaten sind Zuhause.

    Bewusst eng gefasst, denn PC und Mensch koennen an verschiedenen Orten sein:
    - nur Koordinaten (GPS, Windows-Standort) gegen ein WLAN ohne Koordinaten - zwei Koordinaten weit
      auseinander sind nie derselbe Ort;
    - nur WLAN bei wachem PC (im Standby steht der Laptop vielleicht noch im Buero);
    - genau ein Partner: Faellt ein namenloser Ort mit zwei verschiedenen benannten zusammen, bleibt er namenlos.
    """
    partners: dict[str, set[str]] = {}
    for wlan in stays:
        if wlan.ssid is None or wlan.coords is not None or wlan.prio == PRIO_WIFI_STANDBY:
            continue
        for pos in stays:
            if pos.coords is None or min(pos.end, wlan.end) - max(pos.start, wlan.start) < ALIAS_MIN_OVERLAP_MS:
                continue
            k_wlan, k_pos = res.find(wlan.key), res.find(pos.key)
            if k_wlan == k_pos or res.weak(k_wlan) == res.weak(k_pos):
                continue
            weak, strong = (k_pos, k_wlan) if res.weak(k_pos) else (k_wlan, k_pos)
            info = res.info[strong]
            if weak == k_pos and info.lat is not None and not _near(pos.coords, (info.lat, info.lon)):
                continue   # der benannte Ort hat eigene Koordinaten - und die liegen woanders
            partners.setdefault(weak, set()).add(strong)
    for weak, strong in partners.items():
        if len(strong) == 1:
            res.union(weak, next(iter(strong)))


# =========================================================================== Zusammenfuehrung

@dataclass
class _Run:
    kind: str | None           # "stay" | "trip" | None (unbekannt)
    key: str | None
    s0: int
    s1: int
    evs: set = field(default_factory=set)
    filled: list = field(default_factory=list)   # [(s0, s1)] ohne direkten Beleg (erschlossen)


@dataclass
class Segment:
    kind: str                  # "stay" | "trip" | "unknown"
    start: int
    end: int
    name: str | None = None
    known: bool = False        # Name stammt aus den eigenen bekannten Orten
    lat: float | None = None
    lon: float | None = None
    ssid: str | None = None
    sources: list = field(default_factory=list)
    inferred: list = field(default_factory=list)   # [(start, end)] ohne direkten Beleg
    addresses: list = field(default_factory=list)
    from_name: str | None = None
    to_name: str | None = None
    distance_km: float | None = None
    mode: str | None = None
    path: list | None = None
    real_start: int | None = None
    real_end: int | None = None

    @property
    def inferred_ms(self) -> int:
        return sum(max(0, min(e, self.end) - max(s, self.start)) for s, e in self.inferred)

    @property
    def label(self) -> str:
        if self.kind == "stay":
            return self.name or "Unbenannter Ort"
        if self.kind == "trip":
            return MODE_LABELS.get(self.mode or "", "Fahrt")
        return "Ort unbekannt"


def _paint(evidence: list[Evidence], lo: int, n: int) -> list[int]:
    slots = [-1] * n
    order = sorted(range(len(evidence)), key=lambda i: (evidence[i].prio, -(evidence[i].end - evidence[i].start)))
    for i in order:
        e = evidence[i]
        s0 = max(0, round((e.start - lo) / SLOT_MS))
        s1 = min(n, round((e.end - lo) / SLOT_MS))
        if s1 <= s0:
            if s0 >= n:
                continue
            s1 = s0 + 1   # auch kurze Belege belegen mindestens eine Minute
        slots[s0:s1] = [i] * (s1 - s0)
    return slots


def _runs(slots: list[int], evidence: list[Evidence], res: _Resolver) -> list[_Run]:
    runs: list[_Run] = []
    for s, i in enumerate(slots):
        if i < 0:
            kind, key = None, None
        elif evidence[i].kind == "trip":
            kind, key = "trip", None
        else:
            kind, key = "stay", res.find(evidence[i].key)
        if runs and runs[-1].kind == kind and runs[-1].key == key and runs[-1].s1 == s:
            runs[-1].s1 = s + 1
        else:
            runs.append(_Run(kind, key, s, s + 1))
        if i >= 0:
            runs[-1].evs.add(i)
    return runs


class _Strip:
    def __init__(self, evidence: list[Evidence], res: _Resolver, lo: int, n: int):
        self.ev, self.res, self.lo, self.n = evidence, res, lo, n

    # ---- Eigenschaften von Abschnitten
    def coords(self, run: _Run) -> tuple[float, float] | None:
        if run.kind != "stay":
            return None
        info = self.res.place(run.key)
        if info.lat is not None:
            return info.lat, info.lon
        pts = [self.ev[i].coords for i in run.evs if self.ev[i].coords]
        return pts[0] if pts else None

    def trip_end(self, run: _Run, first: bool) -> tuple[float, float] | None:
        trips = sorted((self.ev[i] for i in run.evs if self.ev[i].kind == "trip"), key=lambda e: e.start)
        if not trips:
            return None
        return trips[0].from_pt if first else trips[-1].to_pt

    def same_place(self, a: _Run, b: _Run) -> bool:
        return a.key == b.key or _near(self.coords(a), self.coords(b))

    def stay_at(self, pt: tuple[float, float], s0: int, s1: int) -> _Run:
        key = self.res.key_for_coords(pt[0], pt[1])
        self.res.parent.setdefault(key, key)
        return _Run("stay", self.res.find(key), s0, s1, filled=[(s0, s1)])

    # ---- Luecken schliessen
    def fill(self, gap: _Run, left: _Run | None, right: _Run | None) -> list[_Run]:
        s0, s1 = gap.s0, gap.s1
        length_ms = (s1 - s0) * SLOT_MS
        ext = EXTEND_MAX_MS // SLOT_MS

        def stay(key, a=s0, b=s1):
            return _Run("stay", key, a, b, filled=[(a, b)])

        def trip(a=s0, b=s1):
            return _Run("trip", None, a, b, filled=[(a, b)])

        def unknown(a=s0, b=s1):
            return _Run(None, None, a, b)

        same_max = length_ms <= SAME_PLACE_MAX_MS

        open_ = INFER_TRIP_MAX_MS // SLOT_MS

        def stay_then_unknown(key):
            """Ort davor bekannt, ob danach derselbe: offen - "noch dort" nur kurz, dann unbekannt."""
            cut = min(s1, s0 + open_)
            return [stay(key, s0, cut)] + ([unknown(cut, s1)] if cut < s1 else [])

        def unknown_then_stay(key):
            cut = max(s0, s1 - open_)
            return ([unknown(s0, cut)] if cut > s0 else []) + [stay(key, cut, s1)]

        if left is not None and right is not None:
            if left.kind == "stay" and right.kind == "stay":
                if self.same_place(left, right):
                    return [stay(left.key)] if same_max else [unknown()]
                return [trip()] if length_ms <= INFER_TRIP_MAX_MS else [unknown()]
            if left.kind == "stay" and right.kind == "trip":
                start = self.trip_end(right, first=True)
                if start is not None and self.coords(left) is not None:
                    if _near(self.coords(left), start, TRIP_END_M) and same_max:
                        return [stay(left.key)]      # war dort, bis die Fahrt losging
                    if not _near(self.coords(left), start, TRIP_END_M):
                        return [trip()] if length_ms <= INFER_TRIP_MAX_MS else [unknown()]
                return stay_then_unknown(left.key)  # Abfahrtsort unbekannt: nicht beliebig lange "noch dort"
            if left.kind == "trip" and right.kind == "stay":
                end = self.trip_end(left, first=False)
                if end is not None and self.coords(right) is not None:
                    if _near(end, self.coords(right), TRIP_END_M) and same_max:
                        return [stay(right.key)]     # war schon dort, als die Fahrt endete
                    if not _near(end, self.coords(right), TRIP_END_M):
                        return [trip()] if length_ms <= INFER_TRIP_MAX_MS else [unknown()]
                return unknown_then_stay(right.key)
            # zwischen zwei Fahrten
            end, start = self.trip_end(left, first=False), self.trip_end(right, first=True)
            if _near(end, start, TRIP_END_M) and same_max:
                return [self.stay_at(end, s0, s1)]
            if length_ms <= TRIP_GAP_MAX_MS:
                return [trip()]
            if end is None:
                return [unknown()]
            # Angekommen ist sicher - wie lange er dort blieb, nicht (die naechste Fahrt beginnt woanders)
            cut = min(s1, s0 + INFER_TRIP_MAX_MS // SLOT_MS)
            return [self.stay_at(end, s0, cut)] + ([unknown(cut, s1)] if cut < s1 else [])
        if left is not None:      # nach dem letzten Beleg
            pt = self.coords(left) if left.kind == "stay" else self.trip_end(left, first=False)
            if pt is None and left.kind != "stay":
                return [unknown()]
            cut = min(s1, s0 + ext)
            head = stay(left.key, s0, cut) if left.kind == "stay" else self.stay_at(pt, s0, cut)
            return [head] + ([unknown(cut, s1)] if cut < s1 else [])
        # Vor dem ersten Beleg ueberhaupt (36 Stunden zurueck nichts): Die Daten fangen hier erst an - wo
        # jemand vorher war, laesst sich nicht erschliessen.
        return [unknown()]


def _merge_runs(runs: list[_Run]) -> list[_Run]:
    out: list[_Run] = []
    for r in runs:
        if out and out[-1].kind == r.kind and out[-1].key == r.key and out[-1].s1 == r.s0:
            out[-1].s1 = r.s1
            out[-1].evs |= r.evs
            out[-1].filled += r.filled
        else:
            out.append(r)
    return out


def _absorb_short_stops(runs: list[_Run]) -> list[_Run]:
    """Ein kurzer Halt zwischen zwei Fahrten (Tankstelle, Ampel im WLAN-Bereich) gehoert zur Fahrt."""
    limit = MIN_STOP_IN_TRIP_MS // SLOT_MS
    for i in range(1, len(runs) - 1):
        r = runs[i]
        if (r.kind == "stay" and r.s1 - r.s0 < limit and runs[i - 1].kind == "trip" and runs[i + 1].kind == "trip"):
            r.kind, r.key = "trip", None
    return _merge_runs(runs)


def _drop_noise_trips(runs: list[_Run], strip: "_Strip") -> list[_Run]:
    """Eine kurze "Fahrt" zwischen zwei Aufenthalten am selben Ort ist keine (Parkplatz, GPS-Zittern)."""
    limit = NOISE_TRIP_MS // SLOT_MS
    for i in range(1, len(runs) - 1):
        r, a, b = runs[i], runs[i - 1], runs[i + 1]
        if (r.kind == "trip" and r.s1 - r.s0 < limit and a.kind == "stay" and b.kind == "stay"
                and strip.same_place(a, b)):
            r.kind, r.key = "stay", a.key
            r.evs = {j for j in r.evs if strip.ev[j].kind == "stay"}
    return _merge_runs(runs)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def build_strip(evidence: list[Evidence], places: list[KnownPlace], lo: int, hi: int, *,
                anchors: Iterable[Evidence] = ()) -> list[Segment]:
    """Belege im Zeitraum [lo, hi) -> lueckenlose Folge von Aufenthalten, Fahrten und unbekannten Abschnitten."""
    lo = lo - lo % SLOT_MS
    n = max(1, -(-(hi - lo) // SLOT_MS))
    evidence = [e for e in evidence if e.end >= lo and e.start < hi]
    if not evidence:
        return []
    res = resolve(evidence, places, anchors)
    strip = _Strip(evidence, res, lo, n)
    runs = _runs(_paint(evidence, lo, n), evidence, res)
    filled: list[_Run] = []
    for i, r in enumerate(runs):
        if r.kind is not None:
            filled.append(r)
            continue
        left = runs[i - 1] if i > 0 else None
        right = runs[i + 1] if i + 1 < len(runs) else None
        filled.extend(strip.fill(r, left, right))
    runs = _drop_noise_trips(_absorb_short_stops(_merge_runs(filled)), strip)
    segs = _segments(runs, strip)
    for seg in segs:   # die Minutenleiste rundet auf - nicht ueber das Ende (z. B. "jetzt") hinaus
        seg.end = min(seg.end, hi)
        seg.inferred = [(a, min(b, hi)) for a, b in seg.inferred if a < hi]
    return [seg for seg in segs if seg.end > seg.start]


def _segments(runs: list[_Run], strip: _Strip) -> list[Segment]:
    lo, ev, res = strip.lo, strip.ev, strip.res
    t = lambda s: lo + s * SLOT_MS  # noqa: E731
    segs: list[Segment] = []
    for r in runs:
        evs = sorted((ev[i] for i in r.evs), key=lambda e: e.start)
        seg = Segment(kind=r.kind or "unknown", start=t(r.s0), end=t(r.s1),
                      inferred=[(t(a), t(b)) for a, b in _merge_spans(r.filled)])
        seen: list[str] = []
        for e in evs:
            if e.source not in seen:
                seen.append(e.source)
        seg.sources = [x for x in seen if not (x.endswith(" (PC im Standby)")
                                               and x[:-len(" (PC im Standby)")] in seen)]
        if r.kind == "stay":
            info = res.place(r.key)
            seg.name, seg.known, seg.ssid = info.name, info.known, info.ssid
            pts = [(e.lat, e.lon, e.end - e.start + 1) for e in evs if e.coords]
            if info.known or not pts:
                seg.lat, seg.lon = info.lat, info.lon
            else:   # Mittel der Belege, gewichtet nach Dauer
                w = sum(p[2] for p in pts)
                seg.lat = sum(p[0] * p[2] for p in pts) / w
                seg.lon = sum(p[1] * p[2] for p in pts) / w
            if seg.lat is None:   # ohne Koordinaten laesst sich der Ort nur ueber sein WLAN benennen
                seg.ssid = info.ssid or next((e.ssid for e in evs if e.ssid), None)
            else:
                seg.ssid = None
            seg.addresses = sorted({e.address for e in evs if e.address and e.address != seg.name})
        elif r.kind == "trip":
            trips = [e for e in evs if e.kind == "trip"]
            if trips and not r.filled and all(e.distance_km is not None for e in trips):
                seg.distance_km = round(sum(e.distance_km for e in trips), 2)
            modes = {}
            for e in trips:
                if e.mode:
                    modes[e.mode] = modes.get(e.mode, 0) + (e.end - e.start)
            seg.mode = max(modes, key=modes.get) if modes else None
            path = [pt for e in trips for pt in (e.path or [])]
            seg.path = path or None
        segs.append(seg)
    for i, seg in enumerate(segs):   # Fahrten: woher und wohin
        if seg.kind != "trip":
            continue
        prev = segs[i - 1] if i > 0 else None
        nxt = segs[i + 1] if i + 1 < len(segs) else None
        if prev is not None and prev.kind == "stay":
            seg.from_name = prev.label
        if nxt is not None and nxt.kind == "stay":
            seg.to_name = nxt.label
    return segs


def clip(segments: list[Segment], start: int, end: int) -> list[Segment]:
    """Auf einen Zeitraum zuschneiden; real_start/real_end behalten die vollen Grenzen (z. B. "seit gestern")."""
    out = []
    for seg in segments:
        if seg.end <= start or seg.start >= end:
            continue
        c = Segment(**{k: getattr(seg, k) for k in seg.__dataclass_fields__})
        c.real_start, c.real_end = seg.start, seg.end
        c.start, c.end = max(seg.start, start), min(seg.end, end)
        c.inferred = [(max(s, c.start), min(e, c.end)) for s, e in seg.inferred if e > c.start and s < c.end]
        out.append(c)
    return out


# =========================================================================== Fuer Zeitstrahl und Claude

def strip_for_range(rows: Iterable[dict], start: int, end: int, *, now: int | None = None,
                    places: list[KnownPlace] | None = None,
                    wifi_places: dict[str, str] | None = None,
                    anchor_rows: Iterable[dict] = ()) -> list[Segment]:
    """Standort-Spur fuer [start, end) aus den Ereignissen um diesen Zeitraum herum.

    `rows` sollten [start - LOOKBACK_MS, end + LOOKAHEAD_MS) abdecken (Quellen: EVIDENCE_SOURCES und
    pc_times), damit der Tag mit dem richtigen Ort beginnt und endet; `anchor_rows` (ANCHOR_SOURCES im
    Umkreis ANCHOR_RANGE_MS) liefern zusaetzlich Ortsnamen. Die Zukunft bleibt leer.
    """
    now = timeutil.now_ms() if now is None else now
    end = min(end, now)
    if end <= start:
        return []
    evidence = evidence_from_events(rows, wifi_places=wifi_places)
    anchors = evidence_from_events([dict(r, category="visit") for r in anchor_rows
                                    if r.get("category") in ANCHOR_CATEGORIES])
    hi = min(end + LOOKAHEAD_MS, now)
    return clip(build_strip(evidence, places or [], start - LOOKBACK_MS, hi, anchors=anchors), start, end)


def strip_from_store(events_between, start: int, end: int, cfg, *, now: int | None = None) -> list[Segment]:
    """Standort-Spur direkt aus der Datenbank. `events_between(start, end, sources, categories)` liefert
    Ereigniszeilen (Storage.events_between oder ein Wrapper darum)."""
    rows = events_between(start - LOOKBACK_MS, end + LOOKAHEAD_MS, [*EVIDENCE_SOURCES, PC_TIMES_SOURCE], None)
    # nur Aufenthalte - die Strecken der Fahrten aus vier Wochen braucht es zum Benennen nicht
    anchors = events_between(start - ANCHOR_RANGE_MS, end + ANCHOR_RANGE_MS, list(ANCHOR_SOURCES),
                             list(ANCHOR_CATEGORIES))
    return strip_for_range(rows, start, end, now=now, places=places_from_config(cfg),
                           wifi_places=wifi_places_from_config(cfg), anchor_rows=anchors)


def segment_dict(seg: Segment) -> dict[str, Any]:
    """Anzeigeobjekt fuer den Zeitstrahl (JSON)."""
    dur = seg.end - seg.start
    real_start = seg.real_start if seg.real_start is not None else seg.start
    real_end = seg.real_end if seg.real_end is not None else seg.end
    out: dict[str, Any] = {
        "kind": seg.kind, "ts_start": seg.start, "ts_end": seg.end,
        "real_start": real_start, "real_end": real_end,
        "label": seg.label, "name": seg.name, "known": seg.known,
        "start_label": timeutil.fmt_hm(real_start), "end_label": timeutil.fmt_hm(real_end),
        "starts_before": real_start < seg.start, "ends_after": real_end > seg.end,
        "duration_label": timeutil.human_duration(real_end - real_start),
        "sources": list(seg.sources),
        "inferred": [[s, e] for s, e in seg.inferred], "inferred_ms": seg.inferred_ms,
        "inferred_label": timeutil.human_duration(seg.inferred_ms) if seg.inferred_ms else "",
        "fully_inferred": dur > 0 and seg.inferred_ms >= dur,
    }
    if seg.lat is not None:
        out["lat"], out["lon"] = round(seg.lat, 6), round(seg.lon, 6)
        out["coords_label"] = fmt_coords(seg.lat, seg.lon)
    if seg.ssid:
        out["ssid"] = seg.ssid
    if seg.addresses:
        out["addresses"] = list(seg.addresses)
    if seg.kind == "trip":
        out.update({"from": seg.from_name, "to": seg.to_name, "route": route(seg), "mode": seg.mode,
                    "distance_km": seg.distance_km,
                    "distance_label": fmt_km(seg.distance_km) if seg.distance_km else ""})
        if seg.path:
            out["path"] = seg.path
    return out


def route(seg: Segment) -> str:
    """'Büro → Zuhause', '→ Zuhause' oder 'Büro →' - je nachdem, was bekannt ist."""
    if seg.from_name and seg.to_name:
        return f"{seg.from_name} → {seg.to_name}"
    if seg.to_name:
        return f"→ {seg.to_name}"
    if seg.from_name:
        return f"{seg.from_name} →"
    return ""


def describe(seg: Segment) -> str:
    """Ein Satz fuer Claude und Tooltips: "08:20-16:40 Büro" / "16:40-17:05 Autofahrt Büro -> Zuhause"."""
    start = seg.real_start if seg.real_start is not None else seg.start
    end = seg.real_end if seg.real_end is not None else seg.end
    span = f"{timeutil.fmt_hm(start)}–{timeutil.fmt_hm(end)}"
    if seg.kind == "stay":
        return f"{span} {seg.label}"
    if seg.kind == "trip":
        km = f", {fmt_km(seg.distance_km)}" if seg.distance_km else ""
        return f"{span} {seg.label}{' ' + route(seg) if route(seg) else ''} ({timeutil.human_duration(end - start)}{km})"
    return f"{span} Ort unbekannt"
