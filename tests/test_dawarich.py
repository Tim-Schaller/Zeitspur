"""Dawarich-Standortquelle: Abbildung, Zugangsdaten (DPAPI) und der echte HTTP-Pfad gegen einen Mock."""
import json
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from zeitspur import dawarich, timeutil
from zeitspur.dawarich import (
    DawarichClient,
    DawarichError,
    build_track_events,
    build_visit_event,
    normalize_base_url,
)

TOKEN = "geheim-token-nur-fuer-den-test"


# --------------------------------------------------------------------------- Basis-Adresse

def test_normalize_base_url():
    assert normalize_base_url("example.org") == "https://example.org"
    assert normalize_base_url("https://example.org/") == "https://example.org"
    assert normalize_base_url(" https://example.org/pfad/ ") == "https://example.org/pfad"
    # ueber http waere der Token im Klartext unterwegs -> muss abgelehnt werden
    with pytest.raises(ValueError):
        normalize_base_url("http://example.org")
    with pytest.raises(ValueError):
        normalize_base_url("")


# --------------------------------------------------------------------------- Abbildung

def test_build_visit_event_without_name_uses_coordinates():
    ev = build_visit_event({
        "id": 7, "started_at": "2026-09-16T08:00:00Z", "ended_at": "2026-09-16T09:30:00Z",
        "name": "", "status": "confirmed", "confidence": 0.9,
        "place": {"id": 3, "latitude": 52.51627, "longitude": 13.37770}})
    assert ev.category == "visit" and ev.ext_id == "visit-7"
    assert ev.location == "52.51627, 13.37770"
    assert ev.subject == "Aufenthalt (52.51627, 13.37770)"  # kein Ort geraten
    assert ev.ts_end - ev.ts_start == 90 * 60_000
    extra = json.loads(ev.extra)
    assert extra["latitude"] == 52.51627 and extra["place_id"] == 3 and extra["status"] == "confirmed"


def test_build_visit_event_prefers_name_and_skips_declined():
    named = build_visit_event({"id": 1, "started_at": "2026-09-16T08:00:00Z", "ended_at": "2026-09-16T08:10:00Z",
                               "name": "Büro", "place": {"latitude": 1.0, "longitude": 2.0}})
    assert named.subject == "Büro"
    # verworfene Aufenthalte sind keine Tatsache -> gar nicht uebernehmen
    assert build_visit_event({"id": 2, "started_at": "2026-09-16T08:00:00Z", "status": "declined",
                              "place": {"latitude": 1.0, "longitude": 2.0}}) is None
    assert build_visit_event({"id": 3, "started_at": None}) is None


def test_build_track_event_swaps_geojson_order():
    ev = build_track_events({
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[13.38, 52.52], [13.07, 52.39]]},
        "properties": {"id": 42, "start_at": "2026-09-16T10:00:00Z", "end_at": "2026-09-16T10:30:00Z",
                       "distance": 18500, "avg_speed": 37.0, "duration": 1800}})[0]
    assert ev.category == "track" and ev.ext_id == "track-42"
    # GeoJSON ist [Laengengrad, Breitengrad] - die Breite muss zuerst stehen
    assert ev.location == "52.52000, 13.38000 nach 52.39000, 13.07000"
    assert ev.subject == "Fahrt - 18,5 km"
    extra = json.loads(ev.extra)
    assert extra["distance"] == 18500 and extra["distance_km"] == 18.5 and extra["points"] == 2


def test_track_distance_only_when_plausible():
    base = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[13.0, 52.0], [13.1, 52.1]]},
            "properties": {"id": 1, "start_at": "2026-09-16T10:00:00Z", "end_at": "2026-09-16T10:30:00Z"}}
    # 3400 km in einer halben Stunde ist die falsche Einheit, keine Reise -> keine Entfernung anzeigen
    weird = json.loads(json.dumps(base)); weird["properties"]["distance"] = 3_400_000_000
    assert build_track_events(weird)[0].subject == "Fahrt"
    # unplausibel schnell (600 km in 30 min)
    fast = json.loads(json.dumps(base)); fast["properties"]["distance"] = 600_000
    assert build_track_events(fast)[0].subject == "Fahrt"
    ok = json.loads(json.dumps(base)); ok["properties"]["distance"] = 12_300
    assert build_track_events(ok)[0].subject == "Fahrt - 12,3 km"


# --------------------------------------------------------------------------- Zugangsdaten

@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI nur unter Windows")
def test_credentials_roundtrip(tmp_path):
    path = tmp_path / "dawarich_credentials.bin"
    assert dawarich.load_credentials(path) is None and not dawarich.has_credentials(path)
    dawarich.save_credentials("example.org", TOKEN, path)
    assert dawarich.has_credentials(path)
    assert dawarich.load_credentials(path) == {"base_url": "https://example.org", "token": TOKEN}
    # der Token darf nicht im Klartext auf der Platte stehen
    assert TOKEN.encode() not in path.read_bytes()
    assert dawarich.clear_credentials(path) is True and not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI nur unter Windows")
def test_credentials_reject_http(tmp_path):
    with pytest.raises(ValueError):
        dawarich.save_credentials("http://example.org", TOKEN, tmp_path / "x.bin")


# --------------------------------------------------------------------------- HTTP-Pfad gegen Mock

class _Handler(BaseHTTPRequestHandler):
    visits: list = []
    tracks: list = []
    points: list | None = None   # None: /api/v1/points gibt es nicht (404, wie hinter einem engen Proxy)
    cap: int | None = None       # Server deckelt per_page
    total_header: bool = True
    seen: list = []
    status: int = 200

    def do_GET(self):  # noqa: N802
        _Handler.seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
        if _Handler.status != 200:
            self.send_response(_Handler.status)
            self.end_headers()
            return
        headers = {}
        if self.path.startswith("/api/v1/visits"):
            body = json.dumps(_Handler.visits).encode()
        elif self.path.startswith("/api/v1/tracks"):
            body = json.dumps({"type": "FeatureCollection", "features": _Handler.tracks}).encode()
        elif self.path.startswith("/api/v1/points") and _Handler.points is not None:
            from urllib.parse import parse_qs, urlsplit
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            lo = datetime.fromisoformat(q["start_at"].replace("Z", "+00:00")).timestamp()
            hi = datetime.fromisoformat(q["end_at"].replace("Z", "+00:00")).timestamp()
            hits = [p for p in _Handler.points if lo <= p["timestamp"] <= hi]
            per, page = int(q.get("per_page", 100)), int(q.get("page", 1))
            per = min(per, _Handler.cap or per)
            body = json.dumps(hits[(page - 1) * per:page * per]).encode()
            if _Handler.total_header:
                headers["X-Total-Pages"] = str(max(1, -(-len(hits) // per)))
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # Testausgabe ruhig halten
        pass


@pytest.fixture
def server(monkeypatch):
    _Handler.visits, _Handler.tracks, _Handler.seen, _Handler.status = [], [], [], 200
    _Handler.points, _Handler.cap, _Handler.total_header = None, None, True
    monkeypatch.setattr(DawarichClient, "MIN_REQUEST_INTERVAL_S", 0)   # Tempolimit nur gegen den echten Server
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _client(srv):
    c = DawarichClient({"base_url": "https://platzhalter.invalid", "token": TOKEN})
    # Der Mock spricht http; die https-Pflicht ist oben eigens getestet.
    c.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    return c


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_fetch_maps_both_endpoints_and_sends_token_in_header(server):
    day = date(2026, 9, 16)
    start_ms, end_ms = timeutil.day_bounds(day)
    mid = start_ms + 10 * 3_600_000
    _Handler.visits = [{"id": 1, "started_at": _iso(mid), "ended_at": _iso(mid + 3_600_000),
                        "name": "", "status": "confirmed", "place": {"latitude": 52.5, "longitude": 13.4}}]
    _Handler.tracks = [{"type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[13.4, 52.5], [13.5, 52.6]]},
                        "properties": {"id": 9, "start_at": _iso(mid + 2 * 3_600_000),
                                       "end_at": _iso(mid + 2 * 3_600_000 + 1_800_000), "distance": 15000}}]
    events = _client(server).fetch(day, day)
    assert [e.category for e in events] == ["visit", "track"]
    assert events[0].subject.startswith("Aufenthalt (52.50000")
    assert events[1].subject == "Fahrt - 15,0 km"
    # Token gehoert in den Header - und darf nicht in der URL landen (URLs stehen in Proxy-Logs)
    assert all(r["auth"] == f"Bearer {TOKEN}" for r in _Handler.seen)
    assert all(TOKEN not in r["path"] for r in _Handler.seen)
    assert {r["path"].split("?")[0] for r in _Handler.seen} == {"/api/v1/visits", "/api/v1/tracks"}


def test_fetch_drops_events_outside_the_window(server):
    day = date(2026, 9, 16)
    start_ms, _ = timeutil.day_bounds(day)
    long_ago = start_ms - 5 * 86_400_000
    _Handler.visits = [
        {"id": 1, "started_at": _iso(long_ago), "ended_at": _iso(long_ago + 3_600_000),  # klar davor
         "place": {"latitude": 1.0, "longitude": 2.0}},
        {"id": 2, "started_at": _iso(long_ago), "ended_at": _iso(start_ms + 3_600_000),  # reicht hinein
         "place": {"latitude": 3.0, "longitude": 4.0}},
    ]
    events = _client(server).fetch(day, day)
    assert [e.ext_id for e in events] == ["visit-2"]


def test_fetch_asks_for_visits_earlier_than_the_window(server):
    """Aufenthalte werden nach started_at gefiltert - deshalb muss die Abfrage frueher ansetzen."""
    day = date(2026, 9, 16)
    _client(server).fetch(day, day)
    visits_req = next(r for r in _Handler.seen if r["path"].startswith("/api/v1/visits"))
    tracks_req = next(r for r in _Handler.seen if r["path"].startswith("/api/v1/tracks"))
    import urllib.parse
    v_start = urllib.parse.parse_qs(visits_req["path"].split("?")[1])["start_at"][0]
    t_start = urllib.parse.parse_qs(tracks_req["path"].split("?")[1])["start_at"][0]
    assert v_start < t_start  # Aufenthalte mit Vorlauf, Fahrten exakt


@pytest.mark.parametrize("code,teil", [(401, "401"), (404, "404"), (429, "429"), (502, "502")])
def test_http_errors_are_reported_without_the_token(server, code, teil):
    _Handler.status = code
    with pytest.raises(DawarichError) as exc:
        _client(server).fetch(date(2026, 9, 16), date(2026, 9, 16))
    message = str(exc.value)
    assert teil in message
    assert TOKEN not in message  # Geheimnis darf nie in einer Fehlermeldung stehen


def test_unreachable_server_raises_dawarich_error():
    c = DawarichClient({"base_url": "https://platzhalter.invalid", "token": TOKEN})
    c.base_url = "http://127.0.0.1:9"  # nichts lauscht hier
    with pytest.raises(DawarichError):
        c.fetch(date(2026, 9, 16), date(2026, 9, 16))


def test_certificate_verification_is_never_disabled():
    """Sicherheitsregel: keine abgeschaltete Zertifikatspruefung und kein http-Rueckfall.

    Bei einem TLS-Fehler fehlt dem Server ein gueltiges Zertifikat - das ist ein Fall fuer den
    Betreiber, kein Grund, die Pruefung zu umgehen. Ohne Pruefung waere der Token mitlesbar.
    """
    import inspect

    src = inspect.getsource(dawarich)
    for verboten in ("_create_unverified_context", "CERT_NONE", "check_hostname = False",
                     "check_hostname=False", "verify_mode"):
        assert verboten not in src, f"{verboten} darf im Dawarich-Zugriff nicht vorkommen"
    # und es wird auch kein eigener SSL-Kontext untergeschoben
    assert "ssl" not in [m.split(".")[0] for m in ("import ssl",) if m in src]


# --------------------------------------------------------------------------- Streckenpunkte fuer die Karte

def test_path_keeps_shape_and_caps_points():
    import math
    from zeitspur.dawarich import MAX_PATH_POINTS, _path_from_line
    line = [[11.0 + i * 0.0002, 49.0 + 0.01 * math.sin(i / 40)] for i in range(4000)]
    path = _path_from_line(line)
    assert len(path) <= MAX_PATH_POINTS
    # GeoJSON ist [lon, lat] - gespeichert wird [lat, lon]
    assert path[0] == [round(line[0][1], 5), round(line[0][0], 5)]
    assert path[-1] == [round(line[-1][1], 5), round(line[-1][0], 5)]

    def dist(p, a, b):
        (px, py), (ax, ay), (bx, by) = p, a, b
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    segs = [(tuple(path[i]), tuple(path[i + 1])) for i in range(len(path) - 1)]
    worst = max(min(dist((p[1], p[0]), a, b) for a, b in segs) for p in line)
    assert worst * 111_000 < 25, f"Vereinfachung weicht {worst * 111_000:.0f} m ab"


def test_path_handles_short_and_broken_input():
    from zeitspur.dawarich import _path_from_line
    assert _path_from_line([]) == []
    assert _path_from_line(None) == []
    assert _path_from_line([[11.0, 49.0]]) == [[49.0, 11.0]]
    assert _path_from_line([[11.0, 49.0], ["kaputt"], [11.1, 49.1]]) == [[49.0, 11.0], [49.1, 11.1]]


def test_track_event_carries_path_for_the_map():
    ev = build_track_events({
        "geometry": {"type": "LineString", "coordinates": [[13.0, 52.0], [13.05, 52.05], [13.1, 52.1]]},
        "properties": {"id": 5, "start_at": "2026-09-16T10:00:00Z", "end_at": "2026-09-16T10:20:00Z"}})[0]
    path = json.loads(ev.extra)["path"]
    assert path[0] == [52.0, 13.0] and path[-1] == [52.1, 13.1]


# --------------------------------------------------------------------------- Bekannte Orte

def test_parse_known_place_and_errors():
    from zeitspur.location import DEFAULT_PLACE_RADIUS_M, parse_known_place
    p = parse_known_place("Büro;52.5162746;13.3777041")
    assert p.name == "Büro" and p.radius_m == DEFAULT_PLACE_RADIUS_M
    assert parse_known_place("Kunde;53,55;9,99;300").radius_m == 300  # Komma als Dezimaltrenner
    for bad in ("kaputt", "Name;abc;12", ";49;11", "Name;999;11", "Name;49;11;0"):
        with pytest.raises(ValueError):
            parse_known_place(bad)


def test_distance_and_match_place():
    from zeitspur.location import KnownPlace, distance_m, match_place
    buero = KnownPlace("Büro", 52.5162746, 13.3777041, 150)
    fern = KnownPlace("Kunde", 53.55, 9.99, 300)
    assert 90 < distance_m(52.5162746, 13.3777041, 52.5171267, 13.3777041) < 100  # ~95 m
    assert match_place(52.51633, 13.37775, [buero, fern]).name == "Büro"
    assert match_place(52.5300, 13.4000, [buero, fern]) is None   # ausserhalb jedes Radius
    assert match_place(None, None, [buero]) is None
    # bei mehreren Treffern gewinnt der naechstgelegene
    eng = KnownPlace("Nebenan", 52.51628, 13.37771, 500)
    assert match_place(52.51628, 13.37771, [buero, eng]).name == "Nebenan"


def test_places_from_config_includes_named_map_home():
    from zeitspur.config import Config
    from zeitspur.location import places_from_config
    assert places_from_config(Config()) == []   # ab Werk kein Ort eingebaut
    home = {"map_home_label": "Büro", "map_home_lat": 52.5163, "map_home_lon": 13.3777}
    places = places_from_config(Config(known_places=["Zuhause;52.52;13.41"], **home))
    assert [p.name for p in places] == ["Büro", "Zuhause"]
    # unbrauchbare Eintraege werden uebersprungen statt den Start zu verhindern
    assert [p.name for p in places_from_config(Config(known_places=["kaputt"], **home))] == ["Büro"]


# --------------------------------------------------------------------------- Fahrten aus einem Track
# Aufbau wie in Dawarich-Antworten (mode_timeline mit einem Emoji je Abschnitt); Tage, Zeiten und Orte sind
# erfunden, liegen aber bewusst an den Grenzen von JOURNEY_GAP_S, EDGE_MAX_S und EDGE_GAP_S.

def _sek(iso: str) -> int:
    return timeutil.iso_to_ms(iso) // 1000


def _abschnitt(emoji: str, von: str, bis: str) -> dict:
    return {"emoji": emoji, "start_time": _sek(von), "end_time": _sek(bis)}


def _t(uhrzeit: str, tag: str = "2026-03-02") -> int:
    return timeutil.iso_to_ms(f"{tag}T{uhrzeit}+01:00")


def _track(props: dict) -> dict:
    grund = {"id": 101, "start_at": "2026-03-02T07:29:30+01:00", "end_at": "2026-03-02T18:35:00+01:00",
             "distance": 41200, "duration": 39930, "avg_speed": 3.71, "dominant_mode": "driving"}
    grund.update(props)
    return {"type": "Feature",   # Brandenburger Tor -> Berliner Hauptbahnhof
            "geometry": {"type": "LineString", "coordinates": [[13.37770, 52.51627], [13.36941, 52.52508]]},
            "properties": grund}


def _tag(*abschnitte: tuple[str, str, str], tag: str = "2026-03-02") -> list[dict]:
    return [_abschnitt(e, f"{tag}T{von}+01:00", f"{tag}T{bis}+01:00") for e, von, bis in abschnitte]


# Ein Arbeitstag als ein einziger Track: Hinfahrt, Arbeitstag, Zwischenfahrt, Rueckfahrt - alles in einem.
TAG_ARBEIT = _tag(
    ("🚴", "07:30:00", "07:31:30"),   # Weg zum Auto: 90 s, schliesst direkt an
    ("🚗", "07:31:32", "07:40:00"),
    ("🚶", "07:40:40", "09:00:00"),
    ("🏃", "09:01:00", "11:30:00"),
    ("🚴", "11:30:30", "11:33:00"),
    ("🏃", "12:20:00", "14:59:00"),
    ("🚗", "15:00:00", "15:12:00"),
    ("🚴", "15:12:00", "16:55:00"),   # 103 min "Radfahren" - das Handy liegt auf dem Schreibtisch
    ("📍", "16:58:00", "17:25:00"),
    ("🚴", "17:26:00", "17:27:30"),
    ("🚗", "17:28:00", "17:55:00"),
    ("🚶", "17:57:00", "18:20:00"),
    ("📍", "18:20:30", "18:35:00"),
)


def test_ein_arbeitstag_wird_in_einzelne_fahrten_zerlegt():
    """Aus einem Block von morgens bis abends werden Hinfahrt, Zwischenfahrt und Rueckfahrt."""
    evs = build_track_events(_track({"mode_timeline": TAG_ARBEIT}))
    fenster = [(e.ts_start, e.ts_end) for e in evs]
    assert fenster == [(_t("07:30:00"), _t("07:40:00")), (_t("15:00:00"), _t("15:12:00")),
                       (_t("17:26:00"), _t("17:55:00"))]
    assert [e.ext_id for e in evs] == ["track-101-1", "track-101-2", "track-101-3"]
    # Keine erfundene Teilstrecke, und Start/Ziel des ganzen Tracks waeren je Fahrt irrefuehrend
    assert all(e.subject == "Autofahrt" for e in evs)
    assert all(e.location is None for e in evs)
    extra = json.loads(evs[0].extra)
    assert extra["journey"] == "1/3" and "distance_km" not in extra
    assert extra["track_start"] == "2026-03-02T07:29:30+01:00"


def test_langer_folgeabschnitt_verlaengert_die_fahrt_nicht():
    """103 Minuten "Radfahren" direkt nach dem Anhalten sind das Handy am Schreibtisch."""
    evs = build_track_events(_track({"mode_timeline": TAG_ARBEIT}))
    assert evs[1].ts_end == _t("15:12:00")
    # ... die 90 Sekunden davor dagegen sind der Weg zum Auto und zaehlen mit
    assert evs[0].ts_start == _t("07:30:00")
    assert evs[2].ts_start == _t("17:26:00")


def test_einzelne_fahrt_behaelt_die_entfernung():
    """Solange der Track nur eine Fahrt enthaelt, ist die Entfernung eindeutig zuzuordnen."""
    ev = build_track_events(_track({
        "id": 102, "start_at": "2026-03-03T07:45:00+01:00", "end_at": "2026-03-03T08:00:00+01:00",
        "distance": 6400, "mode_timeline": _tag(
            ("🚴", "07:45:00", "07:46:00"),
            ("🚗", "07:46:05", "07:52:00"),
            ("📍", "07:52:05", "08:00:00"), tag="2026-03-03")}))
    assert len(ev) == 1 and ev[0].subject == "Autofahrt - 6,4 km"
    assert ev[0].ts_end == _t("07:52:00", "2026-03-03")   # ohne Stillstand danach
    assert ev[0].location is not None


def test_aufgeblaehte_entfernung_wird_nicht_gezeigt():
    """Steht das Handy stundenlang, sammelt Dawarich Zitter-Kilometer - die passen zu keiner Fahrt."""
    evs = build_track_events(_track({
        "id": 9, "start_at": "2026-03-03T08:00:00+01:00", "end_at": "2026-03-03T12:00:00+01:00",
        "distance": 30600, "mode_timeline": _tag(
            ("🚗", "08:00:00", "08:06:00"),
            ("📍", "08:06:30", "12:00:00"), tag="2026-03-03")}))
    assert len(evs) == 1 and evs[0].subject == "Autofahrt"   # 30,6 km in 6 min waeren 306 km/h
    assert "distance_km" not in json.loads(evs[0].extra)


def test_zwischenstopp_trennt_keine_fahrt():
    """Neun Minuten Pause unterwegs bleiben eine Fahrt."""
    evs = build_track_events(_track({
        "id": 103, "start_at": "2026-03-07T13:58:00+01:00", "end_at": "2026-03-07T15:25:00+01:00",
        "distance": 81500, "mode_timeline": _tag(
            ("🚗", "14:00:00", "14:33:00"),
            ("📍", "14:35:00", "14:41:00"),
            ("🚗", "14:42:00", "15:14:00"),
            ("🏃", "15:24:00", "15:25:00"), tag="2026-03-07")}))
    assert len(evs) == 1
    assert evs[0].ts_start == _t("14:00:00", "2026-03-07")
    assert evs[0].ts_end == _t("15:14:00", "2026-03-07")


def test_spaziergaenge_ohne_fahrzeug_werden_ebenfalls_getrennt():
    """Ohne Motorabschnitt zaehlen Fussabschnitte - zwei Spaziergaenge sind zwei Eintraege."""
    evs = build_track_events(_track({
        "id": 104, "start_at": "2026-03-08T10:00:00+01:00", "end_at": "2026-03-08T13:05:00+01:00",
        "distance": 2400, "dominant_mode": "walking", "mode_timeline": _tag(
            ("🚶", "10:00:00", "10:30:00"),
            ("📍", "11:30:00", "11:33:00"),
            ("🚶", "13:00:00", "13:05:00"), tag="2026-03-08")}))
    assert [e.subject for e in evs] == ["Zu Fuß", "Zu Fuß"]
    assert evs[0].ts_end == _t("10:30:00", "2026-03-08")
    assert evs[1].ts_start == _t("13:00:00", "2026-03-08")


def test_stehen_ist_keine_fahrt():
    """10 Meter in 51 Minuten fuehrt Dawarich als Fahrt - als Balken waere das schlicht falsch."""
    assert build_track_events(_track({
        "id": 105, "distance": 10, "duration": 3060, "avg_speed": 0.01, "dominant_mode": "unknown",
        "mode_timeline": _tag(("❓", "12:00:00", "12:51:00"), tag="2026-03-07"),
    })) == []
    assert build_track_events(_track({"distance": 300, "mode_timeline": []})) != []


def test_ohne_mode_timeline_bleibt_das_fenster_unveraendert():
    evs = build_track_events(_track({}))
    assert len(evs) == 1
    assert evs[0].ts_start == _t("07:29:30")
    assert evs[0].ts_end == _t("18:35:00")
    assert "track_start" not in json.loads(evs[0].extra)


def test_mode_kind():
    assert dawarich._mode_kind("✈️") == "motor" and dawarich._mode_kind("🚗") == "motor"
    assert dawarich._mode_kind("🚴") == "rad" and dawarich._mode_kind("🚶") == "fuss"
    assert dawarich._mode_kind("📍") == "halt" and dawarich._mode_kind(None) == "halt"


# --------------------------------------------------------------------------- GPS-Rohpunkte

def test_punkte_in_allen_schreibweisen():
    p = dawarich.parse_point({"latitude": "52.52", "longitude": "13.405", "timestamp": 1_788_940_800, "accuracy": 12})
    assert (p.ts, p.lat, p.lon, p.acc) == (1_788_940_800_000, 52.52, 13.405, 12.0)
    wkt = dawarich.parse_point({"lonlat": "POINT (13.405 52.52)", "timestamp": "1788940800"})
    assert (wkt.lat, wkt.lon, wkt.ts) == (52.52, 13.405, 1_788_940_800_000)
    iso = dawarich.parse_point({"latitude": 52.5, "longitude": 13.4, "timestamp": "2026-09-09T08:00:00Z"})
    assert iso.ts == timeutil.iso_to_ms("2026-09-09T08:00:00Z")
    assert dawarich.parse_point({"latitude": None, "longitude": 13.4, "timestamp": 1}) is None
    assert dawarich.parse_point({"latitude": 52.5, "longitude": 13.4}) is None
    assert dawarich.parse_point("kaputt") is None


def test_welche_tage_neu_geholt_werden():
    now = timeutil.to_ms(datetime(2026, 9, 10, 9, 0))                    # Donnerstag 09:00
    d = date(2026, 9, 10)
    fetched = {"2026-09-07": now - 2 * 86_400_000,                       # lange nach Tagesende geholt: fertig
               "2026-09-09": timeutil.day_bounds(date(2026, 9, 9))[1] + 3_600_000}   # erst 01:00: Nachzuegler moeglich
    assert dawarich.days_to_fetch(date(2026, 9, 7), d + timedelta(days=3), fetched, now) == [
        date(2026, 9, 8), date(2026, 9, 9), d]                            # Zukunft nie, heute immer


def _gps_day(day: date):
    """Ein erfundener Arbeitstag als Rohpunkte: zu Hause, Fahrt, Buero, Fahrt, zu Hause."""
    home, office = (52.52, 13.405), (52.50, 13.35)
    base = datetime.combine(day, datetime.min.time()).astimezone()
    t = lambda h, m=0: int((base + timedelta(hours=h, minutes=m)).timestamp())  # noqa: E731
    pts = []

    def stay(at, a, b):
        for ts in range(a, b + 1, 120):
            pts.append({"latitude": at[0], "longitude": at[1], "timestamp": ts, "accuracy": 10})

    def drive(a, b, t0, t1):
        n = (t1 - t0) // 20
        for k in range(1, n):
            pts.append({"latitude": a[0] + (b[0] - a[0]) * k / n, "longitude": a[1] + (b[1] - a[1]) * k / n,
                        "timestamp": t0 + (t1 - t0) * k // n, "accuracy": 8})

    stay(home, t(6), t(7, 55))
    drive(home, office, t(7, 55), t(8, 0))
    stay(office, t(8, 0), t(16, 30))
    drive(office, home, t(16, 30), t(16, 35))
    stay(home, t(16, 35), t(22))
    return pts, t


def test_sync_aus_rohpunkten(server, storage):
    day = date(2026, 9, 9)
    _Handler.points, t = _gps_day(day)
    _Handler.visits = [{"id": 1, "name": "Büro", "status": "suggested",
                        "started_at": datetime.fromtimestamp(t(8, 10), tz=timezone.utc).isoformat(),
                        "ended_at": datetime.fromtimestamp(t(16, 0), tz=timezone.utc).isoformat()}]
    now = timeutil.to_ms(datetime(2026, 9, 11, 12, 0))
    rows = dawarich.sync(_client(server), storage, day, day, now_ms=now)
    stays = [r for r in rows if r["category"] == "visit"]
    trips = [r for r in rows if r["category"] == "track"]
    assert len(stays) == 3 and len(trips) == 2
    assert [json.loads(r["extra"]).get("name") for r in stays] == [None, "Büro", None]   # Name aus Dawarich
    hin = json.loads(trips[0]["extra"])
    assert 3.5 < hin["distance_km"] < 4.5 and hin["mode"] == "car" and len(hin["path"]) >= 2
    assert hin["from"] == pytest.approx([52.52, 13.405], abs=1e-3)
    # Punkte liegen verschluesselt in der eigenen Datenbank, der Tag ist als fertig vermerkt
    start, end = timeutil.day_bounds(day)
    assert len(storage.location_points("dawarich", start, end)) == len(_Handler.points)
    assert json.loads(storage.get_meta(dawarich.POINTS_META_KEY)) == {"2026-09-09": now}
    # Token nur im Header, schlanke Punkte angefordert
    point_calls = [c for c in _Handler.seen if c["path"].startswith("/api/v1/points")]
    assert all(c["auth"] == f"Bearer {TOKEN}" and TOKEN not in c["path"] for c in _Handler.seen)
    assert all("slim=true" in c["path"] for c in point_calls)

    # Zweiter Abgleich: der fertige Tag wird nicht erneut geholt, das Ergebnis bleibt gleich
    _Handler.seen.clear()
    again = dawarich.sync(_client(server), storage, day, day, now_ms=now + 600_000)
    assert [c["path"].split("?")[0] for c in _Handler.seen].count("/api/v1/points") == 1   # nur die Probe
    assert [(r["ts_start"], r["category"]) for r in again] == [(r["ts_start"], r["category"]) for r in rows]


def test_rohpunkte_seitenweise(server, storage, monkeypatch):
    monkeypatch.setattr(dawarich, "POINTS_PER_PAGE", 50)
    day = date(2026, 9, 9)
    _Handler.points, _ = _gps_day(day)
    start, end = timeutil.day_bounds(day)
    got = _client(server).fetch_points(start, end)
    assert len(got) == len(_Handler.points) and [p.ts for p in got] == sorted(p.ts for p in got)


def test_ohne_rohpunkte_bleiben_dawarichs_aufenthalte(server, storage):
    """Laesst ein Proxy /api/v1/points nicht durch, gilt der alte Weg ueber visits und tracks."""
    day = date(2026, 9, 9)
    _Handler.visits = [{"id": 7, "name": "Büro", "status": "suggested", "started_at": "2026-09-09T08:00:00+02:00",
                        "ended_at": "2026-09-09T12:00:00+02:00", "place": {"latitude": 52.5, "longitude": 13.35}}]
    rows = dawarich.sync(_client(server), storage, day, day)
    assert [r["category"] for r in rows] == ["visit"] and json.loads(rows[0]["extra"])["name"] == "Büro"
    assert storage.get_meta(dawarich.POINTS_META_KEY) is None


def test_verbindungstest_nennt_fehlende_rohpunkte(server):
    msg = _client(server).test_connection()
    assert "/api/v1/points" in msg and "Verbindung erfolgreich" in msg
    _Handler.points = []
    assert "Rohpunkte freigegeben" in _client(server).test_connection()


def test_tempolimit_zwischen_anfragen(server, monkeypatch):
    """Dawarich erlaubt 60 Anfragen je Minute - beim Nachholen vieler Tage wird gewartet."""
    waits = []
    monkeypatch.setattr(DawarichClient, "MIN_REQUEST_INTERVAL_S", 1.0)
    monkeypatch.setattr(dawarich.time, "sleep", lambda s: waits.append(s))
    c = _client(server)
    c._get("/api/v1/visits", {})
    c._get("/api/v1/visits", {})
    assert len(waits) == 1 and 0 < waits[0] <= 1.0



@pytest.mark.parametrize("mit_kopf", [True, False])
def test_gedeckelte_seiten_verlieren_keine_punkte(server, mit_kopf):
    """Deckelt der Server (oder ein Proxy) per_page, ist eine kurze Seite noch nicht die letzte."""
    day = date(2026, 9, 9)
    _Handler.points, _ = _gps_day(day)
    _Handler.cap, _Handler.total_header = 100, mit_kopf
    start, end = timeutil.day_bounds(day)
    assert len(_client(server).fetch_points(start, end)) == len(_Handler.points)


def test_langer_aufenthalt_bleibt_beim_abgleich_eines_einzelnen_tags_ganz(server, storage):
    """Montagabend bis Donnerstagfrueh zu Hause: Wer spaeter nur den Dienstag abgleicht, kappt ihn nicht."""
    home = (52.52, 13.405)
    mon = date(2026, 9, 7)
    base = datetime.combine(mon, datetime.min.time()).astimezone()
    t = lambda d, h: int((base + timedelta(days=d, hours=h)).timestamp())  # noqa: E731
    _Handler.points = [{"latitude": home[0], "longitude": home[1], "timestamp": ts, "accuracy": 10}
                       for ts in range(t(0, 18), t(3, 8) + 1, 1800)]
    now = timeutil.to_ms(datetime(2026, 9, 12, 12, 0))
    rows = dawarich.sync(_client(server), storage, mon, mon + timedelta(days=3), now_ms=now)
    storage.replace_events("dawarich", timeutil.day_bounds(mon)[0], timeutil.day_bounds(mon + timedelta(days=3))[1], rows)
    tue = mon + timedelta(days=1)
    again = dawarich.sync(_client(server), storage, tue, tue, now_ms=now)
    assert [(r["ts_start"], r["ts_end"]) for r in again] == [(t(0, 18) * 1000, t(3, 8) * 1000)]


def test_entfernen_waehrend_des_abgleichs_schreibt_nichts_mehr(server, storage):
    day = date(2026, 9, 9)
    _Handler.points, _ = _gps_day(day)
    with pytest.raises(dawarich.Removed):
        dawarich.sync(_client(server), storage, day, day, still_wanted=lambda: False)
    start, end = timeutil.day_bounds(day)
    assert storage.location_points("dawarich", start, end) == [] and storage.get_meta(dawarich.POINTS_META_KEY) is None
