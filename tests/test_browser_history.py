"""Browser-Verlauf: Profile finden, Chromium- und Firefox-Datenbanken lesen, Surf-Phasen bilden, Ausschluesse."""
import json
import re
import sqlite3

import pytest

from zeitspur import browser_history as bh

T = 1_790_000_000_000
MIN = 60_000


def _chromium(path, visits):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    con.execute("CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, transition INTEGER)")
    for i, (ts, url, title, transition) in enumerate(visits, 1):
        con.execute("INSERT INTO urls VALUES (?,?,?)", (i, url, title))
        con.execute("INSERT INTO visits VALUES (?,?,?,?)", (i, i, ts * 1000 + bh.CHROMIUM_OFFSET_US, transition))
    con.commit()
    con.close()


def _firefox(path, visits):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    con.execute("CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER, "
                "visit_type INTEGER)")
    for i, (ts, url, title, kind) in enumerate(visits, 1):
        con.execute("INSERT INTO moz_places VALUES (?,?,?)", (i, url, title))
        con.execute("INSERT INTO moz_historyvisits VALUES (?,?,?,?)", (i, i, ts * 1000, kind))
    con.commit()
    con.close()


@pytest.fixture
def browser(tmp_path):
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    _chromium(local / "Microsoft" / "Edge" / "User Data" / "Default" / "History", [
        (T, "https://www.github.com/beispiel", "GitHub - beispiel", 1),
        (T + MIN, "https://github.com/x/y", "x/y", 0),
        (T + 2 * MIN, "https://ads.example.org/frame", "", 3),             # Unterrahmen zaehlt nicht
        (T + 3 * MIN, "edge://settings/", "Einstellungen", 1),              # interne Seite
        (T + 4 * MIN, "https://docs.python.org/3/", "Python-Doku", 1),
        (T + 60 * MIN, "https://meine-bank.de/konto", "Konto", 1),          # ausgeschlossene Domain
        (T + 61 * MIN, "https://shop.example.org/", "Online-Banking Login", 1),   # Titel passt auf Ausschluss
        (T + 62 * MIN, "https://news.example.org/", "Nachrichten", 1),
    ])
    _chromium(local / "Microsoft" / "Edge" / "User Data" / "Profile 1" / "History", [])
    _firefox(roaming / "Mozilla" / "Firefox" / "Profiles" / "ab12cd34.default-release" / "places.sqlite", [
        (T + 2 * MIN, "https://de.wikipedia.org/wiki/Zeit", "Zeit – Wikipedia", 1),
        (T + 2 * MIN, "https://redirect.example.org/", "", 5),             # Weiterleitung zaehlt nicht
    ])
    return local, roaming


def test_profile_werden_gefunden(browser):
    profiles = bh.find_profiles(*browser)
    assert [(p.browser, p.name, p.engine) for p in profiles] == [
        ("Edge", "Default", "chromium"), ("Edge", "Profile 1", "chromium"), ("Firefox", "default-release", "firefox")]


def test_besuche_lesen_und_umrechnen(browser):
    edge, _, firefox = bh.find_profiles(*browser)
    visits = bh.read_visits(edge, T, T + 10 * MIN)
    assert [v.url for v in visits] == ["https://www.github.com/beispiel", "https://github.com/x/y", "edge://settings/",
                                       "https://docs.python.org/3/"]
    assert visits[0].ts_ms == T and visits[0].browser == "Edge"
    assert [v.title for v in bh.read_visits(firefox, T, T + 10 * MIN)] == ["Zeit – Wikipedia"]


def test_surfphasen_mit_ausschluessen(browser):
    rows = bh.fetch_phases(T - MIN, T + 3 * 60 * MIN, exclude_domains=["meine-bank.de"], store_titles=True,
                           title_patterns=[re.compile(".*Banking.*", re.I)], profiles=bh.find_profiles(*browser))
    assert [(r["ts_start"], r["ts_end"], r["subject"]) for r in rows] == [
        (T, T + 5 * MIN, "github.com, de.wikipedia.org, docs.python.org"),   # gleich oft: zuerst besucht zuerst
        (T + 62 * MIN, T + 63 * MIN, "news.example.org")]
    extra = json.loads(rows[0]["extra"])
    assert extra["domains"] == {"github.com": 2, "docs.python.org": 1, "de.wikipedia.org": 1}
    assert extra["browsers"] == ["Edge", "Firefox"] and rows[0]["category"] == "web"
    assert [p[1] for p in extra["pages"]] == ["github.com", "github.com", "de.wikipedia.org", "docs.python.org"]
    text = json.dumps(rows, ensure_ascii=False)
    assert "meine-bank" not in text and "Banking" not in text


def test_ohne_seitentitel(browser):
    rows = bh.fetch_phases(T - MIN, T + 10 * MIN, exclude_domains=[], store_titles=False, title_patterns=[],
                           profiles=bh.find_profiles(*browser))
    assert "pages" not in json.loads(rows[0]["extra"])


def test_domains_und_unterdomains():
    assert bh.domain_of("https://WWW.Example.org:8443/x") == "example.org"
    assert bh.domain_of("file:///C:/x.html") is None and bh.domain_of("about:blank") is None
    assert bh._excluded("online.bank.de", ["bank.de"]) and not bh._excluded("superbank.de", ["bank.de"])


def test_nichts_lesbar_ist_ein_fehler(tmp_path):
    from zeitspur.plugins import PluginError
    kaputt = bh.Profile("Firefox", "x", tmp_path / "fehlt.sqlite", "firefox")
    with pytest.raises(PluginError, match="nicht lesbar"):
        bh.fetch_phases(T, T + MIN, exclude_domains=[], store_titles=True, title_patterns=[], profiles=[kaputt])


def test_skript_weiterleitungen_und_kettenanfaenge_zaehlen_nicht():
    assert bh._chromium_user_visit(0x30000000)                     # normaler Klick (Kette Anfang+Ende)
    assert bh._chromium_user_visit(0x30000001)                     # eingetippt
    assert not bh._chromium_user_visit(0x40000000)                 # Adresswechsel per Skript (Web-App)
    assert not bh._chromium_user_visit(0x60000000)                 # ... auch am Kettenende
    assert not bh._chromium_user_visit(0x10000000)                 # nur Zwischenstation einer Weiterleitung
    assert not bh._chromium_user_visit(0x30000003)                 # Unterrahmen


def test_neuladen_einmal_und_lange_phasen_geteilt():
    visits = [bh.Visit(T + i * 20_000, "https://claude.ai/chat/1", "Chat", "Edge") for i in range(3)]   # Neuladen
    visits += [bh.Visit(T + i * 4 * MIN, f"https://example.org/{i}", "", "Edge") for i in range(1, 40)]  # 2,6 h am Stueck
    rows = bh.phases(visits)
    assert json.loads(rows[0]["extra"])["domains"]["claude.ai"] == 1
    assert len(rows) == 3 and all(r["ts_end"] - r["ts_start"] <= 61 * MIN for r in rows)
