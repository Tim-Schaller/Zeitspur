"""Browser-Verlauf: besuchte Seiten aus Edge, Chrome, Brave, Vivaldi, Opera und Firefox.

Gelesen wird die Verlaufsdatenbank des Browsers direkt und nur lesend (SQLite mode=ro) - waehrend der Browser
laeuft und ohne Kopie auf der Platte:
    Chromium-Browser: <Profil>\\History        visits.visit_time (Mikrosekunden seit 1601) -> urls
    Firefox:          <Profil>\\places.sqlite  moz_historyvisits.visit_date (Mikrosekunden seit 1970) -> moz_places
InPrivate/Inkognito landet gar nicht erst im Verlauf. Gespeichert werden nicht einzelne Aufrufe, sondern
Surf-Phasen: zusammenhaengende Besuche ohne laengere Pause, mit den meistbesuchten Websites als Betreff und den
Seitentiteln fuer Claude. Seiten, deren Titel auf die Ausschlussliste der Aufnahme passt (etwa Online-Banking),
und ausgeschlossene Domains werden nie gespeichert.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import timeutil

log = logging.getLogger(__name__)

SOURCE = "browser_history"
CHROMIUM_OFFSET_US = 11_644_473_600_000_000   # 1601-01-01 -> 1970-01-01 in Mikrosekunden
GAP_MS = 5 * 60_000            # laengere Pause = neue Surf-Phase
MAX_PHASE_MS = 60 * 60_000     # laengere Phasen werden geteilt - sonst wird ein ganzer Arbeitstag ein Block
REPEAT_MS = 60_000             # dieselbe Adresse innerhalb einer Minute zaehlt einmal (Neuladen)
LAST_VISIT_MS = 60_000         # so lange zaehlt der letzte Aufruf einer Phase noch mit
MAX_PAGES = 40
MAX_SUBJECT_DOMAINS = 3
_CHROMIUM_SKIP = {3, 4}        # Kern-Uebergaenge AUTO_SUBFRAME / MANUAL_SUBFRAME: Inhalte in Rahmen
# Qualifier: Von Skripten ausgeloeste Adresswechsel (CLIENT_REDIRECT) sind keine Besuche - Web-Apps wie claude.ai
# erzeugen so zehntausende Eintraege am Tag. Der Anfang einer Weiterleitungskette (CHAIN_START ohne CHAIN_END)
# ist nur die Zwischenstation; gesehen hat man das Ende der Kette.
_CLIENT_REDIRECT, _CHAIN_START, _CHAIN_END = 0x40000000, 0x10000000, 0x20000000
_FIREFOX_SKIP = {4, 5, 6, 8}   # EMBED, REDIRECT_PERMANENT, REDIRECT_TEMPORARY, FRAMED_LINK


def _chromium_user_visit(transition: int) -> bool:
    if (transition & 0xFF) in _CHROMIUM_SKIP or transition & _CLIENT_REDIRECT:
        return False
    return not (transition & _CHAIN_START and not transition & _CHAIN_END)


@dataclass(frozen=True)
class Profile:
    browser: str
    name: str
    path: Path       # History bzw. places.sqlite
    engine: str      # "chromium" | "firefox"


@dataclass(frozen=True)
class Visit:
    ts_ms: int
    url: str
    title: str
    browser: str


def _dirs() -> tuple[Path, Path]:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    roaming = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    return local, roaming


def find_profiles(local: Path | None = None, roaming: Path | None = None) -> list[Profile]:
    default_local, default_roaming = _dirs()
    local, roaming = local or default_local, roaming or default_roaming
    found: list[Profile] = []
    chromium = (("Edge", local / "Microsoft" / "Edge" / "User Data"),
                ("Chrome", local / "Google" / "Chrome" / "User Data"),
                ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
                ("Vivaldi", local / "Vivaldi" / "User Data"),
                ("Chromium", local / "Chromium" / "User Data"))
    for browser, root in chromium:
        if not root.is_dir():
            continue
        for profile in sorted(root.iterdir()):
            if (profile.name == "Default" or profile.name.startswith("Profile ")) and (profile / "History").is_file():
                found.append(Profile(browser, profile.name, profile / "History", "chromium"))
    for browser, root in (("Opera", roaming / "Opera Software" / "Opera Stable"),
                          ("Opera GX", roaming / "Opera Software" / "Opera GX Stable")):
        if (root / "History").is_file():
            found.append(Profile(browser, "Standard", root / "History", "chromium"))
    firefox = roaming / "Mozilla" / "Firefox" / "Profiles"
    if firefox.is_dir():
        for profile in sorted(firefox.iterdir()):
            if (profile / "places.sqlite").is_file():
                found.append(Profile("Firefox", profile.name.split(".", 1)[-1], profile / "places.sqlite", "firefox"))
    return found


def _open(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro"), uri=True, timeout=3)


def _run(con: sqlite3.Connection, sql: str, params: tuple) -> list[tuple]:
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _query(profile: Profile, sql: str, params: tuple) -> list[tuple]:
    """Liest nur lesend, nie mit Kopie auf der Platte.

    Chromium-Browser halten ihren Verlauf waehrend des Betriebs fast dauerhaft gesperrt (gemessen: Edge mit offenem
    claude.ai-Tab schreibt ununterbrochen). Gelesen wird deshalb ohne Sperren als Momentaufnahme (immutable) - schnell
    und ohne den Browser zu stoeren; nur wenn das einmal scheitert, eine Kopie im Arbeitsspeicher. Firefox (WAL)
    erlaubt gleichzeitiges Lesen und wird normal gelesen."""
    if profile.engine == "chromium":
        for _ in range(2):
            try:
                return _run(_open(profile.path, immutable=True), sql, params)
            except sqlite3.DatabaseError as e:   # mitten in einem Schreibvorgang erwischt
                log.debug("Verlauf %s: Momentaufnahme fehlgeschlagen (%s)", profile.browser, e)
                time.sleep(0.5)
        mem = sqlite3.connect(":memory:")
        mem.deserialize(profile.path.read_bytes())
        return _run(mem, sql, params)
    for attempt in range(2):
        try:
            return _run(_open(profile.path), sql, params)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt:
                raise
            time.sleep(0.5)
    raise sqlite3.OperationalError("database is locked")  # pragma: no cover - Schleife endet vorher


def read_visits(profile: Profile, start_ms: int, end_ms: int) -> list[Visit]:
    if profile.engine == "chromium":
        # Skript-Adresswechsel schon in der Abfrage aussortieren - es sind oft Zehntausende
        rows = _query(profile, "SELECT v.visit_time, u.url, u.title, v.transition FROM visits v "
                               "JOIN urls u ON u.id = v.url WHERE v.visit_time >= ? AND v.visit_time < ? "
                               "AND (v.transition & 0x40000000) = 0 ORDER BY v.visit_time",
                      (start_ms * 1000 + CHROMIUM_OFFSET_US, end_ms * 1000 + CHROMIUM_OFFSET_US))
        return [Visit((t - CHROMIUM_OFFSET_US) // 1000, url or "", title or "", profile.browser)
                for t, url, title, transition in rows if _chromium_user_visit(int(transition or 0))]
    rows = _query(profile, "SELECT v.visit_date, p.url, p.title, v.visit_type FROM moz_historyvisits v "
                           "JOIN moz_places p ON p.id = v.place_id WHERE v.visit_date >= ? AND v.visit_date < ? "
                           "ORDER BY v.visit_date", (start_ms * 1000, end_ms * 1000))
    return [Visit(t // 1000, url or "", title or "", profile.browser)
            for t, url, title, kind in rows if int(kind or 0) not in _FIREFOX_SKIP]


def domain_of(url: str) -> str | None:
    """Website einer Adresse ('www.' entfernt) - nur fuer http/https, sonst None (interne Seiten, Dateien)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    return host[4:] if host.startswith("www.") else host


def _excluded(domain: str, excluded: list[str]) -> bool:
    for d in excluded:
        d = d.strip().lower().removeprefix("www.")
        if d and (domain == d or domain.endswith("." + d)):
            return True
    return False


def phases(visits: list[Visit], *, exclude_domains: list[str] = (), title_patterns=(),
           store_titles: bool = True, gap_ms: int = GAP_MS) -> list[dict]:
    """Besuche -> Surf-Phasen als Zeilen fuer calendar_events."""
    kept: list[tuple[Visit, str]] = []
    last_seen: dict[str, int] = {}
    for v in sorted(visits, key=lambda v: v.ts_ms):
        domain = domain_of(v.url)
        if domain is None or _excluded(domain, list(exclude_domains)):
            continue
        if v.title and any(p.search(v.title) for p in title_patterns):
            continue
        if v.ts_ms - last_seen.get(v.url, -REPEAT_MS - 1) <= REPEAT_MS:
            continue                                   # dieselbe Seite gleich noch einmal (Neuladen)
        last_seen[v.url] = v.ts_ms
        kept.append((v, domain))
    groups: list[list[tuple[Visit, str]]] = []
    for item in kept:
        if (groups and item[0].ts_ms - groups[-1][-1][0].ts_ms <= gap_ms
                and item[0].ts_ms - groups[-1][0][0].ts_ms < MAX_PHASE_MS):
            groups[-1].append(item)
        else:
            groups.append([item])
    rows = []
    for group in groups:
        start = group[0][0].ts_ms
        end = group[-1][0].ts_ms + LAST_VISIT_MS
        counts = Counter(domain for _, domain in group)
        top = [d for d, _ in counts.most_common(MAX_SUBJECT_DOMAINS)]
        subject = ", ".join(top) + (f" +{len(counts) - len(top)}" if len(counts) > len(top) else "")
        extra: dict = {"visits": len(group), "domains": dict(counts.most_common()),
                       "browsers": sorted({v.browser for v, _ in group})}
        if store_titles:
            pages, seen = [], set()
            for v, domain in group:
                key = (domain, v.title)
                if v.title and key not in seen:
                    seen.add(key)
                    pages.append([timeutil.fmt_hm(v.ts_ms), domain, v.title[:200]])
            extra["pages"] = pages[:MAX_PAGES]
        rows.append({"ext_id": f"web-{start}", "ts_start": start, "ts_end": end, "subject": subject,
                     "location": top[0] if top else None, "organizer": None, "attendees": None,
                     "category": "web", "extra": json.dumps(extra, ensure_ascii=False)})
    return rows


def fetch_phases(window_start: int, window_end: int, *, exclude_domains: list[str], store_titles: bool,
                 title_patterns, profiles: list[Profile] | None = None) -> list[dict]:
    from .plugins import PluginError

    visits: list[Visit] = []
    errors = []
    for profile in profiles if profiles is not None else find_profiles():
        try:
            visits.extend(read_visits(profile, window_start, window_end))
        except (sqlite3.Error, OSError) as e:
            log.warning("Verlauf von %s (%s) nicht lesbar: %s", profile.browser, profile.name, e)
            errors.append(f"{profile.browser}: {e}")
    if errors and not visits:
        # Nichts lesbar: lieber Fehler als eine leere Liste - die wuerde gespeicherte Phasen loeschen
        raise PluginError("Browser-Verlauf nicht lesbar (" + "; ".join(errors) + ")")
    return phases(visits, exclude_domains=exclude_domains, title_patterns=title_patterns, store_titles=store_titles)


def describe_profiles() -> str:
    profiles = find_profiles()
    if not profiles:
        return "Kein unterstützter Browser mit Verlauf gefunden."
    names = [f"{p.browser} ({p.name})" if p.name not in ("Default", "Standard") else p.browser for p in profiles]
    return "Bereit. Gefundene Verläufe: " + ", ".join(names) + "."
