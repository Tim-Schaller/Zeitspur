"""Standort-Spur (location.py): GPS-Punkte -> Aufenthalte/Fahrten und die Zusammenfuehrung aller Ortsquellen.

Alle Orte und Koordinaten sind erfunden (Berlin-Mitte als Kulisse).
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from zeitspur import location, timeutil
from zeitspur.location import KnownPlace, Point

DAY = datetime(2026, 9, 9)
ZUHAUSE = (52.52000, 13.40500)
BUERO = (52.50000, 13.35000)      # rund 4,4 km von Zuhause
KUNDE = (52.54000, 13.45000)
PLACES = [KnownPlace("Zuhause", *ZUHAUSE), KnownPlace("Büro", *BUERO)]


def T(h, m=0, day=0):  # noqa: N802
    return timeutil.to_ms(DAY.replace(hour=h, minute=m)) + day * 86_400_000


def stay_points(at, t0, t1, every_s=60, jitter_m=15.0):
    pts, t, i = [], t0, 0
    while t <= t1:
        dlat = ((i * 7) % 5 - 2) * jitter_m / 111_000 / 2
        dlon = ((i * 3) % 5 - 2) * jitter_m / 68_000 / 2
        pts.append(Point(t, at[0] + dlat, at[1] + dlon, 10.0))
        t += every_s * 1000
        i += 1
    return pts


def drive_points(a, b, t0, t1, every_s=20):
    n = max(2, (t1 - t0) // (every_s * 1000))
    return [Point(t0 + (t1 - t0) * k // n, a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n, 8.0)
            for k in range(1, n)]


def row(source, category, t0, t1, subject="", **extra):
    return {"source": source, "category": category, "ts_start": t0, "ts_end": t1, "subject": subject,
            "extra": json.dumps(extra)}


def visit(t0, t1, at=None, name=None):
    x = {"latitude": at[0], "longitude": at[1]} if at else {}
    if name:
        x["name"] = name
    return row("dawarich", "visit", t0, t1, name or "Aufenthalt", **x)


def track(t0, t1, a=None, b=None, km=None, mode="car"):
    x = {"mode": mode}
    if a:
        x["from"] = list(a)
    if b:
        x["to"] = list(b)
    if km is not None:
        x["distance_km"] = km
    return row("dawarich", "track", t0, t1, "Fahrt", **x)


def wifi(t0, t1, ssid, place=None):
    return row("wifi", "wifi", t0, t1, f"WLAN: {ssid}", ssid=ssid, **({"place": place} if place else {}))


def strip(rows, *, start=None, end=None, now=None, places=PLACES, wifi_places=None, anchors=()):
    start = T(0) if start is None else start
    end = T(0, day=1) if end is None else end
    return location.strip_for_range(rows, start, end, now=now if now is not None else T(12, day=3),
                                    places=places, wifi_places=wifi_places, anchor_rows=anchors)


def summary(segs):
    """[(Art, Beginn, Ende, Name/Ziel, erschlossen?)] mit Uhrzeiten - so liest man die Leiste."""
    out = []
    for s in segs:
        name = s.name if s.kind == "stay" else (s.to_name if s.kind == "trip" else None)
        out.append((s.kind, timeutil.fmt_hm(s.start), timeutil.fmt_hm(s.end), name, s.inferred_ms > 0))
    return out


# =========================================================================== Namen und Orte

def test_namen_vergleichen_ohne_rechtsform_und_schreibweise():
    assert location.norm_name("Beispiel GmbH") == location.norm_name("beispiel") == "beispiel"
    assert location.norm_name("Muster GmbH & Co. KG") == "muster"
    assert location.norm_name("Verein Beispiel e.V.") == "verein beispiel"
    assert location.norm_ssid("Firma-WLAN") == location.norm_ssid("firma wlan") == "firmawlan"


def test_adressen_werden_kuerzer():
    assert location.short_address("Supermarkt, Musterweg, 2, Beispielstadt, Bavaria") == \
        "Supermarkt, Musterweg 2, Beispielstadt"
    assert location.short_address("Musterstraße 5") == "Musterstraße 5"


def test_bekannter_ort_hin_und_zurueck():
    p = location.parse_known_place("Büro;52.5;13.35;200")
    assert location.parse_known_place(location.format_known_place(p)) == p


# =========================================================================== GPS-Punkte

def test_ausreisser_und_ungenaue_punkte_fliegen_raus():
    pts = stay_points(ZUHAUSE, T(8), T(8, 10))
    spike = Point(T(8, 5) + 30_000, 52.53, 13.42)            # 1,5 km weg und sofort zurueck
    vague = Point(T(8, 6) + 30_000, 52.52, 13.405, 1500.0)   # Funkzelle
    cleaned = location.clean_points(pts + [spike, vague])
    assert spike not in cleaned and vague not in cleaned and len(cleaned) == len(pts)


def test_arbeitstag_aus_rohpunkten():
    pts = (stay_points(ZUHAUSE, T(6), T(7, 50)) + drive_points(ZUHAUSE, BUERO, T(7, 50), T(8, 5))
           + stay_points(BUERO, T(8, 5), T(16, 30)) + drive_points(BUERO, ZUHAUSE, T(16, 30), T(16, 50))
           + stay_points(ZUHAUSE, T(16, 50), T(18)))
    stays, trips = location.segment_points(pts)
    assert len(stays) == 3 and len(trips) == 2
    # Der Aufenthalt beginnt mit dem ersten Punkt im Umkreis - das kann eine Minute vor dem Halt sein.
    assert [abs(s.start - t) <= 60_000 for s, t in zip(stays, (T(6), T(8, 5), T(16, 50)))] == [True] * 3
    hin = trips[0]
    assert abs(hin.start - T(7, 50)) <= 60_000 and abs(hin.end - T(8, 5)) <= 60_000
    assert 4.0 < hin.distance_m / 1000 < 4.8 and hin.mode is None   # 18 km/h: Rad oder Stadtverkehr
    assert location.distance_m(*hin.from_pt, *ZUHAUSE) < 100 and location.distance_m(*hin.to_pt, *BUERO) < 100
    assert len(hin.path) >= 2


def test_rundfahrt_ohne_halt_bleibt_eine_fahrt():
    """Weg und zurueck an denselben Ort - die Fahrt darf nicht im Aufenthalt verschwinden."""
    pts = (stay_points(ZUHAUSE, T(9), T(10)) + drive_points(ZUHAUSE, KUNDE, T(10), T(10, 5))
           + drive_points(KUNDE, ZUHAUSE, T(10, 5), T(10, 10)) + stay_points(ZUHAUSE, T(10, 10), T(11)))
    stays, trips = location.segment_points(pts)
    assert len(stays) == 2 and len(trips) == 1
    assert trips[0].distance_m > 7000 and trips[0].mode == "car"   # 3,8 km in 5 min: kein Fahrrad


def test_zittern_im_haus_ist_keine_fahrt():
    pts = stay_points(ZUHAUSE, T(9), T(10)) + stay_points((52.5206, 13.4058), T(10, 1), T(11))  # 80 m daneben
    stays, trips = location.segment_points(pts)
    assert len(stays) == 1 and trips == []


def test_seltene_punkte_ergeben_einen_langen_aufenthalt_mit_funkstille():
    """Das Handy meldet sich im Stillstand selten - die lange Stille wird als Luecke vermerkt."""
    pts = [Point(T(8), *BUERO), Point(T(8, 10), *BUERO), Point(T(12), *BUERO), Point(T(12, 5), *BUERO)]
    stays, trips = location.segment_points(pts)
    assert len(stays) == 1 and (stays[0].start, stays[0].end) == (T(8), T(12, 5))
    assert stays[0].parts == [(T(8), T(8, 10)), (T(12), T(12, 5))]


def test_gerade_angekommen_steht_schon_am_ziel():
    pts = stay_points(ZUHAUSE, T(17), T(17, 30)) + drive_points(ZUHAUSE, BUERO, T(17, 30), T(17, 45)) \
        + [Point(T(17, 46), *BUERO), Point(T(17, 47), *BUERO)]
    stays, trips = location.segment_points(pts)
    assert len(trips) == 1 and location.distance_m(stays[-1].lat, stays[-1].lon, *BUERO) < 50


# =========================================================================== Zusammenfuehrung

def test_tag_mit_gps_ist_eine_lueckenlose_leiste():
    rows = [visit(T(0), T(7, 50), ZUHAUSE), track(T(7, 50), T(8, 5), ZUHAUSE, BUERO, 4.4),
            visit(T(8, 5), T(16, 30), BUERO), track(T(16, 30), T(16, 50), BUERO, ZUHAUSE, 4.6),
            visit(T(16, 50), T(23, 59), ZUHAUSE)]
    segs = strip(rows, now=T(23, 59))
    assert summary(segs) == [
        ("stay", "00:00", "07:50", "Zuhause", False), ("trip", "07:50", "08:05", "Büro", False),
        ("stay", "08:05", "16:30", "Büro", False), ("trip", "16:30", "16:50", "Zuhause", False),
        ("stay", "16:50", "23:59", "Zuhause", False)]
    hin = segs[1]
    assert hin.from_name == "Zuhause" and hin.distance_km == 4.4 and hin.label == "Autofahrt"
    assert location.describe(hin) == "07:50–08:05 Autofahrt Zuhause → Büro (15 min, 4,4 km)"


def test_luecke_am_selben_ort_wird_erschlossen():
    """Kein Beleg zwischen zwei Aufenthalten im Buero und keine Fahrt: man war die ganze Zeit dort."""
    segs = strip([visit(T(8), T(10), BUERO), visit(T(13), T(16), BUERO)], start=T(8), end=T(16))
    assert summary(segs) == [("stay", "08:00", "16:00", "Büro", True)]
    assert segs[0].inferred == [(T(10), T(13))] and segs[0].inferred_ms == 3 * 3_600_000


def test_luecke_zwischen_zwei_orten_wird_fahrt_oder_bleibt_unbekannt():
    kurz = strip([visit(T(8), T(16), BUERO), visit(T(16, 40), T(20), ZUHAUSE)], start=T(8), end=T(20))
    assert summary(kurz)[1] == ("trip", "16:00", "16:40", "Zuhause", True)   # nicht aufgezeichnete Heimfahrt
    lang = strip([visit(T(8), T(12), BUERO), visit(T(17), T(20), ZUHAUSE)], start=T(8), end=T(20))
    assert [s.kind for s in lang] == ["stay", "unknown", "stay"]


def test_bis_zur_abfahrt_und_ab_der_ankunft_dort():
    """Zwischen Ankunft und naechster Abfahrt am selben Ort sendet das Handy oft nichts - man war dort."""
    rows = [track(T(7, 50), T(8, 5), ZUHAUSE, BUERO), track(T(16, 30), T(16, 50), BUERO, ZUHAUSE)]
    segs = strip(rows, start=T(7, 50), end=T(16, 50))
    assert summary(segs) == [("trip", "07:50", "08:05", "Büro", False), ("stay", "08:05", "16:30", "Büro", True),
                             ("trip", "16:30", "16:50", "Zuhause", False)]


def test_gps_aufenthalt_schlaegt_ganztages_fahrt_von_dawarich():
    """Dawarichs eigene "Fahrten" umfassen oft einen Einkauf - der erkannte Aufenthalt bleibt sichtbar."""
    rows = [track(T(18), T(19)), visit(T(18, 20), T(18, 35), KUNDE, name="Supermarkt")]
    segs = strip(rows, start=T(18), end=T(19))
    assert [(s.kind, s.name) for s in segs] == [("trip", None), ("stay", "Supermarkt"), ("trip", None)]


def test_kurze_fahrt_vom_ort_zum_selben_ort_ist_rauschen():
    rows = [visit(T(8), T(10), BUERO), track(T(10), T(10, 4)), visit(T(10, 4), T(12), BUERO)]
    assert summary(strip(rows, start=T(8), end=T(12))) == [("stay", "08:00", "12:00", "Büro", False)]


def test_nur_wlan_reicht_fuer_eine_ortsspur():
    """Ohne Handy-GPS: WLAN mit zugeordnetem Ort ergibt die Leiste (wer nur WLAN will, darf das)."""
    rows = [wifi(T(8), T(16), "Firma-Gast"), wifi(T(17), T(23), "Heimnetz")]
    segs = strip(rows, start=T(8), end=T(23), wifi_places={"firmagast": "Büro", "heimnetz": "Zuhause"})
    assert summary(segs) == [("stay", "08:00", "16:00", "Büro", False), ("trip", "16:00", "17:00", "Zuhause", True),
                             ("stay", "17:00", "23:00", "Zuhause", False)]
    assert segs[0].sources == ["WLAN „Firma-Gast“"] and segs[0].known   # ueber den Namen zum bekannten Ort


def test_wlan_name_und_bekannter_ort_mit_rechtsform_sind_derselbe_ort():
    places = [KnownPlace("Beispiel GmbH", *BUERO)]
    rows = [visit(T(8), T(12), BUERO), wifi(T(12), T(16), "Firma"), visit(T(16), T(17), BUERO)]
    segs = strip(rows, start=T(8), end=T(17), places=places, wifi_places={"firma": "Beispiel"})
    assert summary(segs) == [("stay", "08:00", "17:00", "Beispiel GmbH", False)]


def test_unbenannte_koordinaten_lernen_den_namen_vom_wlan():
    """GPS steht an unbekannten Koordinaten, waehrend der PC im WLAN "Zuhause" haengt -> das ist Zuhause."""
    rows = [visit(T(18), T(23), KUNDE), wifi(T(19), T(22), "Heimnetz")]
    segs = strip(rows, start=T(18), end=T(23), places=[], wifi_places={"heimnetz": "Zuhause"})
    assert summary(segs) == [("stay", "18:00", "23:00", "Zuhause", False)]
    assert segs[0].lat == pytest.approx(KUNDE[0])


def test_handy_gps_schlaegt_laptop_im_wlan():
    """Der Laptop haengt zu Hause im WLAN, das Handy ist beim Kunden - der Mensch ist beim Kunden."""
    rows = [wifi(T(8), T(18), "Heimnetz"), track(T(9), T(9, 20), ZUHAUSE, KUNDE),
            visit(T(9, 20), T(11), KUNDE, name="Kunde Beispiel"), track(T(11), T(11, 20), KUNDE, ZUHAUSE)]
    segs = strip(rows, start=T(8), end=T(12), wifi_places={"heimnetz": "Zuhause"})
    assert [(s.kind, s.name) for s in segs] == [("stay", "Zuhause"), ("trip", None), ("stay", "Kunde Beispiel"),
                                                ("trip", None), ("stay", "Zuhause")]


def test_wlan_im_standby_ist_nur_ein_schwacher_beleg():
    pc_on = row("pc_times", "pc_on", T(8), T(12))
    rows = [pc_on, wifi(T(8), T(20), "Heimnetz")]
    segs = strip(rows, start=T(8), end=T(20), wifi_places={"heimnetz": "Zuhause"})
    assert summary(segs) == [("stay", "08:00", "20:00", "Zuhause", False)]
    assert segs[0].sources == ["WLAN „Heimnetz“"]   # Standby-Teil verschmilzt in der Anzeige
    weak = location.evidence_from_events(rows, wifi_places={"heimnetz": "Zuhause"})
    assert sorted((e.prio, e.start, e.end) for e in weak) == [(location.PRIO_WIFI_STANDBY, T(12), T(20)),
                                                             (location.PRIO_WIFI_PLACE, T(8), T(12))]


def test_hotspot_ohne_ort_wird_ignoriert():
    rows = [wifi(T(8), T(9), "Handy-Hotspot")]
    assert strip(rows, start=T(8), end=T(9), wifi_places={"handyhotspot": "-"}) == []


def test_vor_dem_ersten_und_nach_dem_letzten_beleg():
    # Vorher nichts: Die Daten fangen erst an - nichts erschliessen. Danach: noch dort (das Handy schweigt
    # im Stillstand), aber hoechstens EXTEND_MAX_MS.
    segs = strip([visit(T(10), T(12), BUERO)], start=T(0), end=T(0, day=1), now=T(20))
    assert summary(segs) == [("unknown", "00:00", "10:00", None, False), ("stay", "10:00", "20:00", "Büro", True)]
    lang = strip([visit(T(1), T(2), BUERO)], start=T(0), end=T(0, day=1))
    assert [s.kind for s in lang][-1] == "unknown"   # "noch dort" gilt hoechstens EXTEND_MAX_MS


def test_tagesgrenze_zeigt_den_echten_beginn():
    segs = strip([visit(T(18, day=-1), T(7), ZUHAUSE), track(T(7), T(7, 15), ZUHAUSE, BUERO)],
                 start=T(0), end=T(8), now=T(8))
    first = segs[0]
    assert first.start == T(0) and first.real_start == T(18, day=-1)
    d = location.segment_dict(first)
    assert d["starts_before"] and d["start_label"] == "18:00" and d["label"] == "Zuhause"


def test_ortsnamen_aus_anderen_tagen():
    """Am angezeigten Tag kommt der Name nicht vor, Dawarich kennt ihn aber von gestern."""
    rows = [track(T(18, day=-1), T(18, 20, day=-1), BUERO, KUNDE), track(T(7), T(7, 15), KUNDE, BUERO)]
    anchors = [visit(T(19, day=-3), T(22, day=-3), KUNDE, name="Kunde Beispiel")]
    segs = strip(rows, start=T(0), end=T(7, 15), places=[], anchors=anchors)
    assert segs[0].kind == "stay" and segs[0].name == "Kunde Beispiel" and segs[0].real_start == T(18, 20, day=-1)
    ohne = strip(rows, start=T(0), end=T(7, 15), places=[])
    assert ohne[0].kind == "stay" and ohne[0].name is None   # ohne Anker bleibt er unbenannt


def test_keine_belege_keine_leiste():
    assert strip([]) == []
    assert strip([visit(T(8), T(9), BUERO)], start=T(10, day=5), end=T(11, day=5), now=T(9, day=4)) == []


def test_anzeigeobjekt_fuer_den_zeitstrahl():
    rows = [visit(T(8), T(10), BUERO), visit(T(13), T(16), BUERO), track(T(16), T(16, 20), BUERO, ZUHAUSE, 4.6)]
    segs = strip(rows, start=T(8), end=T(16, 20))
    stay, trip = (location.segment_dict(s) for s in segs)
    assert stay["label"] == "Büro" and stay["known"] and stay["fully_inferred"] is False
    assert stay["inferred"] == [[T(10), T(13)]] and stay["inferred_label"] == "3 h 0 min"
    assert stay["coords_label"] == "52.50000, 13.35000"
    # Ziel ist der erschlossene Aufenthalt nach der Ankunft - auch wenn er ausserhalb des Ausschnitts liegt
    assert trip["from"] == "Büro" and trip["to"] == "Zuhause" and trip["distance_label"] == "4,6 km"
    json.dumps([stay, trip])   # muss sich an die Oberflaeche schicken lassen


def test_unbenannter_ort_ohne_koordinaten_laesst_sich_ueber_das_wlan_benennen():
    segs = strip([wifi(T(8), T(9), "Gastnetz")], start=T(8), end=T(9), wifi_places={})
    d = location.segment_dict(segs[0])
    assert d["name"] is None and d["label"] == "Unbenannter Ort" and d["ssid"] == "Gastnetz" and "lat" not in d


def test_unbekannter_abfahrtsort_heisst_nicht_beliebig_lange_noch_dort():
    """Fahrt ohne bekannten Start (Dawarichs zerlegte Tages-Tracks): nur kurz "noch dort", keine Nacht im Buero."""
    rows = [visit(T(7), T(8), BUERO), track(T(15, day=1), T(15, 20, day=1))]
    segs = strip(rows, start=T(7), end=T(15, 20, day=1), now=T(16, day=1))
    assert [s.kind for s in segs] == ["stay", "unknown", "trip"]
    assert segs[0].end == T(8) + location.INFER_TRIP_MAX_MS


def test_derselbe_ort_nach_tagen_ohne_daten_bleibt_unbekannt():
    rows = [visit(T(7), T(8), BUERO), visit(T(9, day=4), T(10, day=4), BUERO)]
    segs = strip(rows, start=T(7), end=T(10, day=4), now=T(11, day=4))
    assert [s.kind for s in segs] == ["stay", "unknown", "stay"]



# --------------------------------------------------------------------------- Befunde aus dem Review

def test_stille_nacht_ist_keine_fahrt():
    """Das Handy schweigt die Nacht ueber; der erste Punkt morgens liegt schon 550 m weg (unterwegs)."""
    pts = stay_points(ZUHAUSE, T(20), T(22)) + [Point(T(7, 30, day=1), 52.5150, 13.4000)] \
        + drive_points((52.5150, 13.4000), BUERO, T(7, 30, day=1), T(7, 45, day=1)) \
        + stay_points(BUERO, T(7, 45, day=1), T(9, day=1))
    stays, trips = location.segment_points(pts)
    assert len(trips) == 1 and trips[0].start == T(7, 30, day=1)
    assert location.distance_m(*trips[0].from_pt, *ZUHAUSE) < 50   # Abfahrt von zu Hause bleibt bekannt


def test_pc_im_standby_benennt_das_zuhause_nicht():
    pc_on = row("pc_times", "pc_on", T(8), T(17))
    rows = [pc_on, wifi(T(8), T(9, day=1), "Firmennetz"), visit(T(18), T(7, 30, day=1), KUNDE)]
    segs = strip(rows, start=T(18), end=T(7, 30, day=1), places=[], wifi_places={"firmennetz": "Firma"})
    assert segs[0].kind == "stay" and segs[0].name is None


def test_koordinaten_widersprechen_einem_bekannten_ort():
    """Desktop bleibt im Buero an (Windows-Standort), das Handy ist zu Hause - zu Hause ist nicht das Buero."""
    rows = [row("windows_location", "visit", T(17), T(23, 59), latitude=BUERO[0], longitude=BUERO[1]),
            visit(T(18), T(23, 59), KUNDE)]
    segs = strip(rows, start=T(18), end=T(23), places=[KnownPlace("Büro", *BUERO)])
    assert [(s.kind, s.name) for s in segs] == [("stay", None)]


def test_kein_alias_ueber_zwei_benannte_orte_hinweg():
    rows = [visit(T(8), T(20), KUNDE), wifi(T(8), T(12), "Heimnetz"), wifi(T(13), T(20), "Firmennetz")]
    segs = strip(rows, start=T(8), end=T(20), places=[], wifi_places={"heimnetz": "Zuhause", "firmennetz": "Firma"})
    assert [(s.kind, s.name) for s in segs] == [("stay", None)]


def test_zwei_bekannte_orte_gleichen_namens_bleiben_zwei_orte():
    places = [KnownPlace("Beispiel GmbH", *BUERO), KnownPlace("Beispiel AG", *KUNDE)]
    rows = [visit(T(8), T(12), BUERO), visit(T(12, 30), T(16), KUNDE)]
    segs = strip(rows, start=T(8), end=T(16), places=places)
    assert [(s.kind, s.name) for s in segs] == [("stay", "Beispiel GmbH"), ("trip", None), ("stay", "Beispiel AG")]


# --------------------------------------------------------------------------- Handy zeichnet nur in Bewegung auf

def test_dichte_punkte_werden_als_autofahrt_erkannt():
    """Ein Punkt je Sekunde: die Geschwindigkeit ueber 30-s-Abschnitte, nicht ueber Paare oder Ampelstopps."""
    pts = drive_points(ZUHAUSE, BUERO, T(8), T(8, 6), every_s=1)   # 4,4 km in 6 min, ~44 km/h
    assert location.classify_mode(pts)[0] == "car"


def test_ankunft_stille_und_neue_fahrt_woanders():
    """Fahrt ins Buero, dann einen Tag nichts, dann Abfahrt woanders: keine 30-Stunden-Fahrt."""
    pts = (drive_points(ZUHAUSE, BUERO, T(8), T(8, 6), every_s=2) + [Point(T(8, 6), *BUERO)]
           + drive_points(KUNDE, ZUHAUSE, T(15, day=1), T(15, 10, day=1), every_s=2))
    stays, trips = location.segment_points(pts)
    assert [timeutil.fmt_hm(t.start) for t in trips] == ["08:00", "15:00"]
    assert trips[0].end <= T(8, 6) and trips[1].end > T(15, 8, day=1)   # nicht ueber die Stille hinweg
    rows = [track(t.start, t.end, t.from_pt, t.to_pt) for t in trips]
    segs = strip(rows, start=T(8), end=T(15, 10, day=1), now=T(16, day=1))
    assert [(s.kind, s.name, s.inferred_ms > 0) for s in segs] == [
        ("trip", None, False), ("stay", "Büro", True), ("unknown", None, False), ("trip", None, False)]
    assert segs[1].end == T(8, 6) + location.INFER_TRIP_MAX_MS   # angekommen - wie lange, ist offen


def test_abfahrt_nach_der_nacht_vom_aufenthalt_davor():
    """Das Handy beginnt erst unterwegs zu senden - der Start ist trotzdem zu Hause."""
    pts = (stay_points(ZUHAUSE, T(20), T(22)) + drive_points((52.5150, 13.4000), BUERO, T(7, 30, day=1), T(7, 45, day=1))
           + [Point(T(7, 45, day=1), *BUERO)])
    stays, trips = location.segment_points(pts)
    assert location.distance_m(*trips[0].from_pt, *ZUHAUSE) < 50
    rows = [visit(s.start, s.end, (s.lat, s.lon)) for s in stays] + [track(t.start, t.end, t.from_pt, t.to_pt) for t in trips]
    segs = strip(rows, start=T(20), end=T(7, 45, day=1), now=T(8, day=1))
    assert [(s.kind, s.name) for s in segs][:2] == [("stay", "Zuhause"), ("trip", None)]


def test_dawarichs_ortsnamen_benennen_ohne_eigenen_aufenthalt():
    from zeitspur import dawarich

    visits = [{"id": 3, "name": "Kunde Beispiel", "status": "suggested", "place": {"latitude": KUNDE[0], "longitude": KUNDE[1]},
               "started_at": "2026-09-01T09:00:00+02:00", "ended_at": "2026-09-01T11:00:00+02:00"}]
    rows = dawarich.events_from_points([], visits, T(0, day=-30), T(0, day=1))
    assert [r["category"] for r in rows] == ["named_place"]
    anchors = [dict(r, source="dawarich") for r in rows]
    segs = strip([track(T(7), T(7, 20), ZUHAUSE, KUNDE), track(T(9), T(9, 20), KUNDE, ZUHAUSE)],
                 start=T(7), end=T(9, 20), places=[], anchors=anchors)
    assert [s.name for s in segs if s.kind == "stay"] == ["Kunde Beispiel"]
    assert location.evidence_from_events(anchors) == []   # selbst kein Beleg fuer die Leiste
