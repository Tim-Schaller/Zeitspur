"""ZeitspurMCP.exe - lokaler MCP-Server (stdio, read-only) fuer Claude.

Der Server wird von Claude Desktop / Claude Code bei Bedarf gestartet, oeffnet die verschluesselte
Datenbank nur lesend (Schluessel per DPAPI wie der Dienst) und stellt Werkzeuge bereit, mit denen
Claude vergangene Aktivitaeten abfragen kann. stdout ist der JSON-RPC-Kanal: Logging geht
ausschliesslich in %LOCALAPPDATA%\\Zeitspur\\logs\\mcp.log.

Zusatzfunktionen:
  --register-claude-desktop   traegt den Server in claude_desktop_config.json ein (mit Backup); nur bei
                              beendetem Claude Desktop, sonst Rueckgabewert 4
  --print-config              zeigt die Konfigurationsschnipsel fuer Claude Desktop / Claude Code
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import logging.handlers
import os
import shutil
import sys
import threading
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import APP_NAME, __version__, edition, plugins, timeutil
from .config import Config, ConfigError, key_path, load_config, logs_dir
from .crypto import KeyProtectionError, WrongKeyError, unprotect_key
from .storage import OCR_DONE, OCR_FAILED, OCR_PENDING, ReadOnlyStorage

log = logging.getLogger("zeitspur.mcp")

SERVER_NAME = "Zeitspur"
_INSTRUCTIONS_BASE = (
    "Zeitspur ist der lokale, verschlüsselte Aktivitätsverlauf des Windows-PCs dieses Nutzers: "
    "regelmäßige Screenshots mit erkanntem Bildschirmtext (OCR), Fenstertitel und Programmname. "
    "Alle Zeiten sind lokale Zeit (ISO 8601). Rufe zuerst get_time_context auf, um 'heute', 'gestern' "
    "oder Wochentage aufzulösen. get_activity_at beantwortet 'Was habe ich am Mittwoch um 12 Uhr gemacht?', "
    "search_activity findet Stichworte im Bildschirmtext, list_active_apps liefert die Tagesübersicht, "
    "get_entry den vollständigen Text eines Eintrags und get_screenshot das zugehörige Bild. "
    "get_calendar liefert die Ereignisse der installierten Plugins eines Tages - je nach Einrichtung Termine "
    "(Outlook, ICS-Kalender), Anrufe und Gespräche (Teams, Zoom, Telefon-Apps, Meetings im Browser), gesendete "
    "und empfangene Mails, Windows-Mitteilungen, Surf-Phasen im Browser, Git-Commits, GitHub-Aktivität, die "
    "Zeiten, in denen der PC an war, und WLAN-Verbindungen. Weitere Angaben stehen je Ereignis unter 'details' "
    "(z. B. die Seitentitel einer Surf-Phase, die Textvorschau einer Mitteilung, ob ein Gespräch mit Video lief). "
    "get_activity_at und list_active_apps enthalten diese Ereignisse unter 'calendar', sodass sich "
    "Bildschirmaktivität einem Meeting, Anruf oder einer Mail zuordnen lässt. "
    "Antworte auf 'Was habe ich um X gemacht?' als EINE zusammenhaengende Aussage: unter 'calendar' stehen "
    "Termine, Gespräche und die anderen Ereignisse, unter 'blocks'/'entries' die Bildschirmarbeit. Also etwa: "
    "'Du hast in Visual Studio an X gearbeitet; laut Outlook lief parallel der Termin Y.' Ein WLAN-Ereignis mit "
    "details.place nennt den Ort, den der Nutzer diesem Netz zugeordnet hat (z. B. 'Firma') - den darfst du "
    "nennen ('du warst in der Firma'); ohne place nur den Netznamen nennen und keinen Ort raten."
)
# Nur in Ausgaben mit Standort-Historie (siehe edition.py) - der Release-Build kennt keine Orte.
_INSTRUCTIONS_LOCATIONS = (
    " Ist die Standort-Historie eingerichtet, enthaelt 'calendar' auch Aufenthalte (Kategorie 'Aufenthalt') "
    "und Fahrten ('Fahrt'), und get_activity_at liefert unter 'location' den Ort ('place' = benannter Ort wie "
    "'Buero', sonst null mit 'coordinates'). Dann gehoert der Ort in dieselbe Aussage: 'Du warst im Buero "
    "und hast in Visual Studio an X gearbeitet; laut Outlook lief parallel der Kundentermin Y.' "
    "Ist 'place' null, nenne die Koordinaten (z. B. 'Koordinaten 48.14, 11.58') und rate keine Adresse; eine "
    "vermutete Adresse klar als Vermutung kennzeichnen. Fehlt 'location' ganz, sage nichts ueber den Ort - "
    "dann liegen einfach keine Standortdaten vor."
)


def instructions() -> str:
    return _INSTRUCTIONS_BASE + (_INSTRUCTIONS_LOCATIONS if edition.LOCATIONS else "")


WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
OCR_LABELS = {OCR_PENDING: "ausstehend", OCR_DONE: "erkannt", OCR_FAILED: "fehlgeschlagen"}
EVENT_CATEGORY_LABELS = plugins.CATEGORY_LABELS
MAX_ENTRIES = 60
# Zusatzangaben der Plugins, die Claude zu einem Ereignis bekommt (unter 'details')
DETAIL_KEYS = ("app", "text", "video", "window_titles", "in_progress", "domains", "pages", "visits", "repo", "hash",
               "type", "url", "commits", "folder", "start_reason", "end_reason", "ssid", "place", "calendar", "all_day")


DIRECTION_LABELS = {"outgoing": "ausgehend", "incoming": "eingehend"}


def _event_coords(e: dict[str, Any]) -> tuple[float, float] | None:
    try:
        extra = json.loads(e.get("extra") or "{}")
        return float(extra["latitude"]), float(extra["longitude"])
    except (ValueError, TypeError, KeyError):
        return None


def _source_name(source: str | None) -> str:
    from . import plugins
    try:
        return plugins.get(source or "").name
    except plugins.UnknownPlugin:
        return source or ""


def _fmt_event(e: dict[str, Any], places=None) -> dict[str, Any]:
    out = {
        "source": e.get("source"),
        "source_label": _source_name(e.get("source")),
        "category": EVENT_CATEGORY_LABELS.get(e.get("category"), e.get("category")),
        "subject": e.get("subject") or "",
        "start": timeutil.iso_local(e["ts_start"]),
        "end": timeutil.iso_local(e["ts_end"]),
        "duration": timeutil.human_duration(e["ts_end"] - e["ts_start"]),
    }
    try:
        extra = json.loads(e.get("extra") or "{}")
    except (ValueError, TypeError):
        extra = {}
    extra = extra if isinstance(extra, dict) else {}
    direction = extra.get("direction")
    if direction:
        out["direction"] = DIRECTION_LABELS.get(direction, direction)
    if e.get("source") != "dawarich":   # Orte der Standort-Historie haben ihre eigene Aufbereitung (unten)
        details = {k: extra[k] for k in DETAIL_KEYS if extra.get(k) not in (None, "", [], {})}
        if details:
            out["details"] = details
    for key in ("location", "organizer", "attendees"):
        if e.get(key):
            out[key] = e[key]
    # Aus Koordinaten einen Namen machen, wenn der Ort bekannt ist ("Buero" statt "52.51627, ...").
    coords = _event_coords(e) if e.get("source") == "dawarich" else None
    if coords and places and edition.LOCATIONS:
        from .dawarich import match_place

        hit = match_place(coords[0], coords[1], places)
        if hit is not None:
            out["place"] = hit.name
            out["subject"] = hit.name if out["category"] == "Aufenthalt" else out["subject"]
    if coords and "place" not in out:
        # Ehrlich bleiben: unbekannter Ort -> Koordinaten nennen, keine Adresse raten.
        out["place"] = None
    return out


def _location_at(raw_events, center_ms: int, places) -> dict[str, Any] | None:
    """Wo war der Nutzer zu diesem Zeitpunkt? Beantwortet 'wo war ich' ohne dass die KI suchen muss.

    Bevorzugt den Aufenthalt, der den Zeitpunkt umschliesst; sonst eine laufende Fahrt. Ist der Ort nicht
    bekannt, bleibt place=None und es stehen nur die Koordinaten da - bewusst, statt eine Adresse zu raten.
    """
    if not edition.LOCATIONS:
        return None
    from .dawarich import match_place

    best = None
    for e in raw_events or []:
        if e.get("source") != "dawarich":
            continue
        covers = e["ts_start"] <= center_ms <= e["ts_end"]
        rank = (0 if e.get("category") == "visit" else 1, 0 if covers else 1)
        if best is None or rank < best[0]:
            best = (rank, e)
    if best is None:
        return None
    rank, e = best
    out: dict[str, Any] = {
        "kind": "Aufenthalt" if e.get("category") == "visit" else "Fahrt",
        "covers_timestamp": rank[1] == 0,
        "from": timeutil.iso_local(e["ts_start"]),
        "to": timeutil.iso_local(e["ts_end"]),
        "coordinates": e.get("location"),
        "place": None,
    }
    coords = _event_coords(e)
    if coords:
        hit = match_place(coords[0], coords[1], places)
        if hit is not None:
            out["place"] = hit.name
    return out


class ActivityReader:
    """Oeffnet die Datenbank lazy und nur lesend; bei Fehlern wird beim naechsten Aufruf neu versucht."""

    def __init__(self, cfg: Config, key: bytes | None = None):
        self.cfg = cfg
        self._key = key
        self._store: ReadOnlyStorage | None = None
        self._lock = threading.RLock()

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        kp = key_path()
        if not kp.exists():
            raise RuntimeError("Zeitspur ist noch nicht eingerichtet (kein Schlüssel gefunden). "
                               "Bitte zuerst Zeitspur starten und die Ersteinrichtung abschließen.")
        try:
            self._key = unprotect_key(kp.read_bytes())
        except KeyProtectionError as e:
            raise RuntimeError(f"Der Datenbankschlüssel kann nicht entschlüsselt werden: {e}") from e
        return self._key

    def store(self) -> ReadOnlyStorage:
        with self._lock:
            if self._store is None:
                db = self.cfg.resolved_db_path
                if not db.exists():
                    raise RuntimeError(f"Die Datenbank {db} existiert noch nicht. Läuft Zeitspur?")
                try:
                    self._store = ReadOnlyStorage(db, self._load_key())
                except WrongKeyError as e:
                    raise RuntimeError(f"Datenbank nicht lesbar: {e}") from e
            return self._store

    def reset(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None

    def call(self, fn, *args, **kwargs):
        """Fuehrt eine Leseoperation aus; bei DB-Fehlern wird die Verbindung einmal neu aufgebaut."""
        try:
            return fn(self.store(), *args, **kwargs)
        except RuntimeError:
            raise
        except Exception as e:  # z. B. Datei ausgetauscht/zurueckgesetzt
            log.warning("Lesefehler, Verbindung wird neu aufgebaut: %s", e)
            self.reset()
            return fn(self.store(), *args, **kwargs)


# --------------------------------------------------------------------------- Formatierung

def _range_bounds(text: str | None, *, end: bool) -> int | None:
    """Datum -> Tagesgrenze (Ende inklusive), Datum+Zeit -> exakter Zeitpunkt."""
    if not text or not str(text).strip():
        return None
    s = str(text).strip()
    if len(s) <= 10 or s.lower() in ("heute", "today", "gestern", "yesterday"):
        start, stop = timeutil.day_bounds(timeutil.parse_date(s))
        return stop if end else start
    return timeutil.to_ms(timeutil.parse_when(s))


def _fmt_entry(row: dict[str, Any], *, max_text_chars: int | None) -> dict[str, Any]:
    text = row.get("ocr_text") or ""
    truncated = False
    if max_text_chars is not None and len(text) > max_text_chars:
        text, truncated = text[:max_text_chars].rstrip() + " …", True
    out: dict[str, Any] = {
        "entry_id": row["id"],
        "start": timeutil.iso_local(row["ts_start"]),
        "end": timeutil.iso_local(row["ts_end"]),
        "duration": timeutil.human_duration(row["ts_end"] - row["ts_start"]),
        "app": row.get("process_name") or "",
        "window_title": row.get("window_title") or "",
        "monitor": row.get("monitor_id"),
        "ocr_status": OCR_LABELS.get(row.get("ocr_status"), ""),
    }
    if "ocr_text" in row:
        out["text"] = text
        if truncated:
            out["text_truncated"] = True
    return out


def _blocks(rows: list[dict[str, Any]], gap_ms: int) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for r in rows:
        cur = blocks[-1] if blocks else None
        if (cur and cur["_app"] == r["process_name"] and cur["_title"] == r["window_title"]
                and r["ts_start"] - cur["_end"] <= gap_ms):
            cur["_end"] = max(cur["_end"], r["ts_end"])
            cur["entries"] += 1
            cur["entry_ids"].append(r["id"])
        else:
            blocks.append({"_app": r["process_name"], "_title": r["window_title"], "_start": r["ts_start"],
                           "_end": r["ts_end"], "entries": 1, "entry_ids": [r["id"]]})
    return [{
        "app": b["_app"], "window_title": b["_title"], "start": timeutil.iso_local(b["_start"]),
        "end": timeutil.iso_local(b["_end"]), "duration": timeutil.human_duration(b["_end"] - b["_start"]),
        "entries": b["entries"], "entry_ids": b["entry_ids"][:20],
    } for b in blocks]


# --------------------------------------------------------------------------- Werkzeuge

class ActivityTools:
    """Werkzeug-Implementierungen (ohne MCP-Dekoratoren, direkt testbar)."""

    def __init__(self, reader: ActivityReader):
        self._places_cache: list | None = None
        self.reader = reader

    def get_time_context(self) -> dict[str, Any]:
        ts = time.time()
        now = datetime.fromtimestamp(ts).astimezone()
        info: dict[str, Any] = {
            "now": now.isoformat(timespec="seconds"),
            "today": now.date().isoformat(),
            "weekday": WEEKDAYS[now.weekday()],
            "utc_offset": now.strftime("%z"),          # z. B. +0200 (time.tzname ist unter Windows oft falsch kodiert)
            # Nicht now.dst(): astimezone() ohne Zone liefert einen festen Offset, dessen dst() immer None ist.
            # tm_isdst ist -1, wenn das System es nicht weiss - dann lieber False.
            "is_dst": time.localtime(ts).tm_isdst > 0,
            "yesterday": (now.date() - timedelta(days=1)).isoformat(),
            "last_7_days": {WEEKDAYS[(now.date() - timedelta(days=i)).weekday()]: (now.date() - timedelta(days=i)).isoformat()
                            for i in range(1, 8)},
            "retention_days": self.reader.cfg.retention_days,
            "capture_interval_seconds": self.reader.cfg.capture_interval_seconds,
            "hint": "Zeitangaben als 'YYYY-MM-DD HH:MM' (lokale Zeit) an get_activity_at übergeben.",
        }
        try:
            st = self.reader.call(lambda s: s.stats())
            info.update(
                total_entries=st["entries"], ocr_pending=st["pending_ocr"],
                first_recorded=timeutil.iso_local(st["oldest_ms"]) if st["oldest_ms"] else None,
                last_recorded=timeutil.iso_local(st["newest_ms"]) if st["newest_ms"] else None,
                recorded_days=self.reader.call(lambda s: s.list_days()),
            )
        except RuntimeError as e:
            info["database"] = f"nicht verfügbar: {e}"
        return info

    def search_activity(self, query: str, from_date: str | None = None, to_date: str | None = None,
                        limit: int = 20) -> dict[str, Any]:
        if not query or not query.strip():
            raise ValueError("query darf nicht leer sein")
        from_ms = _range_bounds(from_date, end=False)
        to_ms = _range_bounds(to_date, end=True)
        limit = max(1, min(int(limit), 200))
        rows = self.reader.call(lambda s: s.search(query, from_ms=from_ms, to_ms=to_ms, limit=limit))
        results = []
        for r in rows:
            item = _fmt_entry(r, max_text_chars=None)
            # Treffer-Marker (STX/ETX aus dem FTS-snippet) fuer die Textausgabe in lesbare [ ] wandeln
            item["snippet"] = " ".join((r.get("snippet") or "").replace("\x02", "[").replace("\x03", "]").split())
            item["relevance"] = round(-float(r.get("score") or 0.0), 3)  # bm25: kleiner = besser, daher negiert
            results.append(item)
        return {"query": query, "from": timeutil.iso_local(from_ms) if from_ms else None,
                "to": timeutil.iso_local(to_ms) if to_ms else None, "count": len(results), "results": results,
                "hint": "Treffer im Textausschnitt sind mit [ ] markiert. Vollständiger Text: get_entry(entry_id)."}

    def get_activity_at(self, timestamp: str, window_minutes: int = 15, include_text: bool = True,
                        max_text_chars: int = 1200) -> dict[str, Any]:
        if not timestamp or not str(timestamp).strip():
            raise ValueError("timestamp fehlt (z. B. '2026-09-09 12:00')")
        center = timeutil.to_ms(timeutil.parse_when(str(timestamp)))
        window_ms = max(1, min(int(window_minutes), 12 * 60)) * 60_000
        rows = self.reader.call(lambda s: s.entries_around(center, window_ms, with_text=include_text))
        gap = 3 * self.reader.cfg.interval_ms
        blocks = _blocks(rows, gap)
        truncated = False
        if len(rows) > MAX_ENTRIES:
            rows = sorted(sorted(rows, key=lambda r: abs(r["ts_start"] - center))[:MAX_ENTRIES], key=lambda r: r["ts_start"])
            truncated = True
        entries = [_fmt_entry(r, max_text_chars=max(50, int(max_text_chars)) if include_text else None) for r in rows]
        raw_events = self.reader.call(lambda s: s.events_around(center, window_ms))
        events = [_fmt_event(e, self.places()) for e in raw_events]
        result: dict[str, Any] = {
            "timestamp": timeutil.iso_local(center), "window_minutes": window_ms // 60_000,
            "from": timeutil.iso_local(center - window_ms), "to": timeutil.iso_local(center + window_ms),
            "count": len(rows), "entries_truncated": truncated,
        }
        if edition.LOCATIONS:   # ohne Standort-Funktionen gibt es das Feld gar nicht
            result["location"] = _location_at(raw_events, center, self.places())
        return {
            **result,
            "calendar": events, "blocks": blocks, "entries": entries,
        }

    def places(self) -> list:
        """Bekannte Orte aus der Konfiguration (einmal je Prozess)."""
        if not edition.LOCATIONS:
            return []
        if self._places_cache is None:
            from .dawarich import places_from_config

            self._places_cache = places_from_config(self.reader.cfg)
        return self._places_cache

    def list_active_apps(self, day: str = "heute", top_titles: int = 5) -> dict[str, Any]:
        d = timeutil.parse_date(str(day))
        start, end = timeutil.day_bounds(d)
        stats = self.reader.call(lambda s: s.app_stats(start, end))
        apps = []
        for st in stats:
            titles = self.reader.call(lambda s: s.title_stats(start, end, st["process_name"], limit=max(0, int(top_titles))))
            apps.append({
                "app": st["process_name"], "duration": timeutil.human_duration(st["total_ms"]),
                "duration_ms": int(st["total_ms"]), "entries": st["entries"], "distinct_windows": st["titles"],
                "first_seen": timeutil.iso_local(st["first_ms"]), "last_seen": timeutil.iso_local(st["last_ms"]),
                "top_windows": [{"window_title": t["window_title"], "duration": timeutil.human_duration(t["total_ms"]),
                                 "entries": t["entries"], "first_seen": timeutil.iso_local(t["first_ms"]),
                                 "last_seen": timeutil.iso_local(t["last_ms"])} for t in titles],
            })
        total_ms = sum(a["duration_ms"] for a in apps)
        events = [_fmt_event(e, self.places()) for e in self.reader.call(lambda s: s.events_between(start, end))]
        return {"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()],
                "total_entries": self.reader.call(lambda s: s.count_between(start, end)),
                "active_duration": timeutil.human_duration(total_ms), "apps": apps, "calendar": events}

    def get_calendar(self, day: str = "heute") -> dict[str, Any]:
        d = timeutil.parse_date(str(day))
        start, end = timeutil.day_bounds(d)
        events = [_fmt_event(e, self.places()) for e in self.reader.call(lambda s: s.events_between(start, end))]
        return {"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()], "count": len(events), "events": events}

    def get_entry(self, entry_id: int) -> dict[str, Any]:
        row = self.reader.call(lambda s: s.get_entry(int(entry_id)))
        if row is None:
            raise ValueError(f"Kein Eintrag mit entry_id {entry_id}")
        out = _fmt_entry(row, max_text_chars=None)
        prev_id, next_id = self.reader.call(lambda s: s.neighbor_ids(int(entry_id)))
        out["previous_entry_id"], out["next_entry_id"] = prev_id, next_id
        return out

    def get_screenshot_bytes(self, entry_id: int, max_width: int = 1280) -> tuple[bytes, str]:
        from PIL import Image as PILImage

        webp = self.reader.call(lambda s: s.get_image(int(entry_id)))
        if webp is None:
            raise ValueError(f"Kein Screenshot für entry_id {entry_id}")
        img = PILImage.open(io.BytesIO(webp))
        max_width = max(320, min(int(max_width), 3840))
        if img.width > max_width:
            img.thumbnail((max_width, max_width * 4))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80, optimize=True)
        return buf.getvalue(), "jpeg"


def build_server(reader: ActivityReader) -> MCPServer:
    tools = ActivityTools(reader)
    mcp = MCPServer(SERVER_NAME, instructions=instructions(), version=__version__)

    @mcp.tool()
    def get_time_context() -> dict:
        """Aktuelle lokale Zeit, Zeitzone, heutiges Datum, Wochentage der letzten 7 Tage sowie Umfang der
        aufgezeichneten Daten (erster/letzter Eintrag, aufgezeichnete Tage). Zuerst aufrufen, um relative
        Zeitangaben wie 'gestern' oder 'letzten Mittwoch 12 Uhr' aufzulösen."""
        return tools.get_time_context()

    @mcp.tool()
    def search_activity(query: str, from_date: str | None = None, to_date: str | None = None, limit: int = 20) -> dict:
        """Volltextsuche im erkannten Bildschirmtext und in Fenstertiteln (Präfixsuche, Umlaute egal,
        Phrasen in Anführungszeichen). from_date/to_date optional als 'YYYY-MM-DD' (Tag inklusive) oder
        'YYYY-MM-DD HH:MM' (lokale Zeit). Liefert je Treffer entry_id, Zeit, App, Fenstertitel, Textausschnitt."""
        return tools.search_activity(query, from_date, to_date, limit)

    @mcp.tool()
    def get_activity_at(timestamp: str, window_minutes: int = 15, include_text: bool = True,
                        max_text_chars: int = 1200) -> dict:
        """Alle Einträge rund um einen Zeitpunkt (± window_minutes), chronologisch, gruppiert in Aktivitätsblöcke
        (App + Fenster + Dauer) und mit erkanntem Text je Eintrag. timestamp als 'YYYY-MM-DD HH:MM' (lokale Zeit).
        Hauptwerkzeug für 'Was habe ich am ... um ... Uhr gemacht?'. Enthält zusätzlich 'calendar'
        (Ereignisse der installierten Plugins, z. B. Termine und Anrufe), sodass sich Termin und
        Bildschirmarbeit in einer Antwort verbinden lassen."""
        return tools.get_activity_at(timestamp, window_minutes, include_text, max_text_chars)

    @mcp.tool()
    def list_active_apps(day: str = "heute", top_titles: int = 5) -> dict:
        """Tagesübersicht: welche Programme und Fenster an einem Tag ('YYYY-MM-DD', 'heute', 'gestern') wie lange
        und wie oft aktiv waren, mit den häufigsten Fenstertiteln je Programm. Enthält unter 'calendar' auch die
        Ereignisse der installierten Plugins (z. B. Outlook-Termine, Teams-Anrufe) des Tages."""
        return tools.list_active_apps(day, top_titles)

    @mcp.tool()
    def get_calendar(day: str = "heute") -> dict:
        """Ereignisse eines Tages ('YYYY-MM-DD', 'heute', 'gestern') aus den installierten Plugins, z. B.
        Outlook-Termine und Teams-Anrufe, mit Betreff, Zeit, Dauer, Ort, Organisator und Teilnehmern.
        Nützlich, um Bildschirmaktivität einem Meeting oder Anruf zuzuordnen."""
        return tools.get_calendar(day)

    @mcp.tool()
    def get_entry(entry_id: int) -> dict:
        """Vollständiger erkannter Text und Metadaten eines einzelnen Eintrags (entry_id aus search_activity oder
        get_activity_at), inklusive Nachbar-Einträgen zum Weiterblättern."""
        return tools.get_entry(entry_id)

    @mcp.tool()
    def get_screenshot(entry_id: int, max_width: int = 1280) -> Image:
        """Screenshot zu einem Eintrag (entry_id). Wird im Speicher entschlüsselt, auf max_width Pixel verkleinert
        und als JPEG zurückgegeben."""
        data, fmt = tools.get_screenshot_bytes(entry_id, max_width)
        return Image(data=data, format=fmt)

    return mcp


# --------------------------------------------------------------------------- Registrierung / CLI

def server_command() -> dict[str, Any]:
    """Startkommando fuer Claude-Clients: gepackte EXE oder Python-Modul im Entwicklungsmodus.

    Gepackt: ZeitspurMCP.exe direkt; laeuft die Registrierung ueber Zeitspur.exe, wird
    'Zeitspur.exe --mcp' eingetragen (dieselbe Funktion, unabhaengig von einer zweiten EXE)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if exe.name.lower() == "zeitspurmcp.exe":
            return {"command": str(exe)}
        return {"command": str(exe), "args": ["--mcp"]}
    root = Path(__file__).resolve().parents[1]
    return {"command": str(Path(sys.executable).resolve()), "args": ["-m", "zeitspur.mcp_server"],
            "env": {"PYTHONPATH": str(root)}}


# Claude Desktop gibt es in zwei Ausgaben, die ihre Konfiguration an verschiedenen Orten lesen:
#  - Microsoft Store / WinGet / MSIX-Paket: Programm unter ...\WindowsApps\Claude_<Version>_x64__<Kennung>\.
#    Das Paket laeuft in einem App-Container: Dateien, die es unter %APPDATA% neu anlegt, legt Windows in
#    %LOCALAPPDATA%\Packages\Claude_<Kennung>\LocalCache\Roaming\ ab und liest sie bevorzugt von dort.
#    Eine Datei unter %APPDATA%\Claude sieht diese Ausgabe dann nicht.
#  - klassisches Setup: Programm unter %LOCALAPPDATA%\AnthropicClaude\, Konfiguration unter %APPDATA%\Claude.
CLAUDE_CONFIG_NAME = "claude_desktop_config.json"
# Paketfamilienname = Paketname + "_" + Herausgeber-Kennung. Paketnamen enthalten keinen Unterstrich, das
# Muster trifft also genau Pakete namens "Claude" - ohne die Kennung fest einzubauen.
CLAUDE_PACKAGE_PATTERN = "Claude_*"
# Rueckgabewert von --register-claude-desktop, solange Claude Desktop laeuft (installer.iss wertet ihn aus)
EXIT_CLAUDE_DESKTOP_RUNNING = 4


def _env_dir(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name) or Path.home() / "AppData" / fallback)


def claude_desktop_package_dirs() -> list[Path]:
    """Paketordner der Store-Ausgabe von Claude Desktop (%LOCALAPPDATA%\\Packages\\Claude_<Kennung>)."""
    try:
        return sorted(p for p in (_env_dir("LOCALAPPDATA", "Local") / "Packages").glob(CLAUDE_PACKAGE_PATTERN)
                      if p.is_dir())
    except OSError:
        return []


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def claude_desktop_config_paths() -> list[Path]:
    """Die claude_desktop_config.json-Dateien, die Claude Desktop liest - die der Store-Ausgabe zuerst.

    Vorhandene Dateien kommen alle zurueck: Beide Ausgaben koennen nebeneinander installiert sein, oder eine
    fruehere Registrierung hat %APPDATA%\\Claude angelegt. Gibt es noch keine, die anzulegende Datei: im
    Paketordner, wenn Claude Desktop aus dem Store stammt, sonst unter %APPDATA%\\Claude.
    Zeigen zwei Pfade auf dieselbe Datei (laeuft dieser Prozess selbst im Container von Claude Desktop, leitet
    Windows auch %APPDATA%\\Claude dorthin um), zaehlt sie nur einmal - sonst ueberschriebe die zweite
    Sicherung .bak die erste mit dem bereits geaenderten Stand.
    """
    store = [d / "LocalCache" / "Roaming" / "Claude" / CLAUDE_CONFIG_NAME for d in claude_desktop_package_dirs()]
    classic = _env_dir("APPDATA", "Roaming") / "Claude" / CLAUDE_CONFIG_NAME
    found: list[Path] = []
    for path in (*store, classic):
        if path.is_file() and not any(_same_file(path, seen) for seen in found):
            found.append(path)
    return found or store or [classic]


def is_claude_desktop_exe(path: str | None) -> bool:
    """Gehoert diese Programmdatei zu Claude Desktop (Store- oder klassische Ausgabe)?

    Claude Code heisst ebenfalls claude.exe - eigenstaendig installiert oder von Claude Desktop unter
    %APPDATA%\\Claude\\claude-code\\ mitgebracht - und zaehlt nicht: Es schreibt claude_desktop_config.json nicht.
    """
    p = (path or "").replace("/", "\\").lower()
    return p.endswith("\\claude.exe") and ("\\windowsapps\\claude_" in p or "\\anthropicclaude\\" in p)


def claude_desktop_running() -> bool:
    """Laeuft Claude Desktop? Dann haelt es seine Konfiguration im Speicher und schreibt die ganze Datei bei
    naechster Gelegenheit (etwa geaenderten Einstellungen) neu - ein Eintrag von aussen ginge verloren.
    Am 04.10.2026 so beobachtet: Ein von Hand ergaenzter Server war nach wenigen Minuten wieder weg."""
    import psutil

    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() != "claude.exe":
            continue
        try:
            if is_claude_desktop_exe(proc.exe()):
                return True
        except psutil.Error:
            continue
    return False


def _load_claude_config(path: Path) -> dict[str, Any]:
    # utf-8-sig: von Hand bearbeitete Dateien tragen oft ein BOM, json.loads wuerde daran scheitern
    raw = path.read_text(encoding="utf-8-sig").strip() if path.exists() else ""
    if not raw:
        return {"mcpServers": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} ist kein gültiges JSON ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} enthält kein JSON-Objekt")
    if not isinstance(data.setdefault("mcpServers", {}), dict):
        raise ValueError(f"{path}: mcpServers ist kein Objekt")
    return data


def register_claude_desktop(config_file: Path | None = None) -> list[Path]:
    """Traegt den Server unter mcpServers.Zeitspur ein - in config_file oder in jede Datei, die Claude Desktop
    liest (claude_desktop_config_paths). Bestehende Eintraege bleiben erhalten, vorher wird je Datei eine
    Sicherung <Datei>.bak angelegt. Erst werden alle Dateien gelesen und geprueft, dann geschrieben: Ist eine
    kaputt, bleiben alle unveraendert.

    Laeuft Claude Desktop, ueberschreibt es die Aenderung wieder - vorher claude_desktop_running() pruefen
    (register_cli tut das)."""
    targets = [Path(config_file)] if config_file else claude_desktop_config_paths()
    configs = [(path, _load_claude_config(path)) for path in targets]
    command = server_command()
    for path, data in configs:
        data["mcpServers"][SERVER_NAME] = command
        if path.exists():
            shutil.copy2(path, path.with_suffix(".json.bak"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return targets


def config_snippets() -> str:
    cmd = server_command()
    desktop = json.dumps({"mcpServers": {SERVER_NAME: cmd}}, indent=2, ensure_ascii=False)
    parts = [cmd["command"], *cmd.get("args", [])]
    code = f"claude mcp add {SERVER_NAME} -- " +" ".join(f'"{p}"' if " " in p else p for p in parts)
    files = "\n".join(f"  {p}" for p in claude_desktop_config_paths())
    return (f"Claude Desktop – Konfigurationsdatei:\n{files}\n"
            f"(Claude Desktop vorher vollständig beenden, sonst überschreibt es die Änderung)\n{desktop}\n\n"
            f"Claude Code:\n{code}\n")


MB_ICONERROR = 0x10
MB_RETRYCANCEL_WARNING = 0x05 | 0x30   # MB_RETRYCANCEL | MB_ICONWARNING
IDRETRY = 4

CLAUDE_RUNNING_TEXT = (
    "Claude Desktop läuft gerade. Es hält seine Einstellungen im Speicher und schreibt sie bei nächster "
    "Gelegenheit zurück – ein Eintrag für Zeitspur würde dabei wieder überschrieben.\n\n"
    "Bitte Claude Desktop vollständig beenden: mit der rechten Maustaste auf das Claude-Symbol im Infobereich "
    "der Taskleiste klicken und „Beenden“ wählen. Das Fenster nur zu schließen genügt nicht, dann läuft "
    "Claude Desktop im Hintergrund weiter.")


def _notify(text: str, *, quiet: bool, flags: int = 0x40) -> None:
    """Meldung an den Nutzer: in der Konsole als Text, als Fenster-Programm (ohne stdout) als Meldungsfenster."""
    if sys.stdout:
        print(text)
    elif not quiet:
        from . import winutil

        winutil.message_box(text, flags=flags)


def register_cli(*, quiet: bool) -> int:
    """--register-claude-desktop: nur bei beendetem Claude Desktop eintragen und das Ergebnis melden.

    Claude Desktop wird bewusst nie selbst beendet (offene Unterhaltungen, laufende Antworten). Als
    Fenster-Programm gibt es "Wiederholen"; die Konsole und der Installer (--quiet) bekommen
    EXIT_CLAUDE_DESKTOP_RUNNING und fragen selbst nach."""
    from . import winutil

    while claude_desktop_running():
        log.warning("Claude Desktop laeuft - Registrierung nicht geschrieben")
        if sys.stdout or quiet:
            _notify(CLAUDE_RUNNING_TEXT + "\n\nDanach den Befehl erneut ausführen.", quiet=quiet)
            return EXIT_CLAUDE_DESKTOP_RUNNING
        if winutil.message_box(CLAUDE_RUNNING_TEXT + "\n\nDanach auf „Wiederholen“ klicken.",
                               flags=MB_RETRYCANCEL_WARNING) != IDRETRY:
            return EXIT_CLAUDE_DESKTOP_RUNNING
    try:
        paths = register_claude_desktop()
    except (OSError, ValueError) as e:
        log.error("Registrierung in Claude Desktop fehlgeschlagen: %s", e)
        _notify(f"Zeitspur konnte nicht in Claude Desktop eingetragen werden:\n\n{e}", quiet=quiet, flags=MB_ICONERROR)
        return 1
    files = "\n".join(str(p) for p in paths)
    log.info("Claude-Desktop-Konfiguration aktualisiert: %s", ", ".join(str(p) for p in paths))
    _notify(f"Zeitspur wurde in Claude Desktop eingetragen:\n\n{files}\n\n"
            "Claude Desktop jetzt wieder starten – dann steht Zeitspur dort bereit.", quiet=quiet)
    return 0


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    try:
        log_dir = logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "mcp.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(fh)
    except OSError:
        root.addHandler(logging.NullHandler())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ZeitspurMCP", description="Zeitspur MCP-Server (stdio, read-only)")
    p.add_argument("--register-claude-desktop", action="store_true", help="in claude_desktop_config.json eintragen (Claude Desktop vorher ganz beenden)")
    p.add_argument("--print-config", action="store_true", help="Konfigurationsschnipsel ausgeben")
    p.add_argument("--quiet", action="store_true", help="keine Meldungsfenster (fuer den Installer)")
    p.add_argument("--version", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    warnings.simplefilter("ignore")  # nichts darf auf stdout/stderr landen
    args = parse_args(argv)
    _setup_logging()
    if args.version:
        if sys.stdout:
            print(f"{APP_NAME} MCP {__version__}")
        return 0
    if args.register_claude_desktop:
        return register_cli(quiet=args.quiet)
    if args.print_config:
        _notify(config_snippets(), quiet=args.quiet)
        return 0
    if sys.stdin is None or sys.stdout is None:
        from . import winutil

        winutil.message_box("ZeitspurMCP ist der MCP-Server für Claude und wird von Claude selbst gestartet.\n\n"
                            "Zum Einrichten: ZeitspurMCP.exe --register-claude-desktop\n"
                            "oder in Claude Code: claude mcp add Zeitspur --\"<Pfad>\\ZeitspurMCP.exe\"")
        return 2
    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("config.yaml ungueltig, Standardwerte: %s", e)
        cfg = Config()
    # Der MCP-Server wird von Claude gestartet und laeuft damit in dessen App-Container. Existiert dort
    # eine umgeleitete Kopie der Datenbank, wuerde er daraus antworten - also veraltete Aktivitaeten
    # melden, ohne dass es jemand merkt. Keine Antwort ist besser als eine falsche.
    from . import winutil

    shadow = winutil.container_shadow(cfg.resolved_db_path)
    if shadow:
        log.error("Datenbank wird in einen App-Container umgeleitet: %s -> %s - MCP-Server startet nicht, "
                  "sonst wuerde er aus einer veralteten Kopie antworten.", cfg.resolved_db_path, shadow)
        return 3
    reader = ActivityReader(cfg)
    server = build_server(reader)
    log.info("MCP-Server startet (stdio), DB %s", cfg.resolved_db_path)
    try:
        server.run(transport="stdio")
    finally:
        reader.reset()
        log.info("MCP-Server beendet")
    return 0


def run() -> None:
    try:
        code = main()
    except SystemExit:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Unbehandelter Fehler im MCP-Server")
        code = 1
    # os._exit vermeidet haengende Threads/MessageBoxen des noconsole-Bootloaders - vorher Puffer leeren
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    logging.shutdown()
    os._exit(code)


if __name__ == "__main__":
    run()
