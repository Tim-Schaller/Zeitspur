"""SQLCipher-Zugriffsschicht: Schema, Schreib-/Lesemethoden, FTS5-Volltextsuche, Wartung.

Alle Zeitstempel sind Unix-Millisekunden (UTC). Screenshots liegen als WebP-BLOBs in der
Tabelle `images` (1:1 zu `entries`, ON DELETE CASCADE). Die FTS5-Tabelle ist eine
External-Content-Tabelle ueber `entries` und wird per Trigger synchron gehalten; der
UPDATE-Trigger reagiert bewusst nur auf ocr_text/window_title, nicht auf ts_end.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import sqlcipher3

from . import timeutil
from .crypto import open_database

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4
OCR_PENDING, OCR_DONE, OCR_FAILED = 0, 1, 2
MAX_OCR_ATTEMPTS = 3              # danach bleibt ein Eintrag endgueltig ohne Text
EVENT_COLUMNS = ("id, source, ext_id, ts_start, ts_end, subject, location, organizer, attendees, "
                 "category, extra, synced_at")
DEFAULT_MAX_SPAN_MS = 6 * 3_600_000  # obere Schranke fuer die Laenge eines Eintrags (Index-Nutzung)

SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS entries(
        id INTEGER PRIMARY KEY,
        ts_start INTEGER NOT NULL,
        ts_end INTEGER NOT NULL,
        monitor_id INTEGER NOT NULL DEFAULT 1,
        process_name TEXT NOT NULL DEFAULT '',
        window_title TEXT NOT NULL DEFAULT '',
        exe_path TEXT,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        ocr_status INTEGER NOT NULL DEFAULT 0,
        ocr_text TEXT,
        ocr_conf REAL,
        created_at INTEGER NOT NULL,
        ocr_attempts INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS ix_entries_ts_start ON entries(ts_start)",
    "CREATE INDEX IF NOT EXISTS ix_entries_ocr_pending ON entries(id) WHERE ocr_status = 0",
    """CREATE TABLE IF NOT EXISTS images(
        entry_id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
        webp BLOB NOT NULL)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
        ocr_text, window_title,
        content='entries', content_rowid='id',
        tokenize='unicode61 remove_diacritics 2')""",
    """CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
        INSERT INTO entries_fts(rowid, ocr_text, window_title) VALUES (new.id, new.ocr_text, new.window_title);
    END""",
    """CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE OF ocr_text, window_title ON entries BEGIN
        INSERT INTO entries_fts(entries_fts, rowid, ocr_text, window_title)
            VALUES ('delete', old.id, old.ocr_text, old.window_title);
        INSERT INTO entries_fts(rowid, ocr_text, window_title) VALUES (new.id, new.ocr_text, new.window_title);
    END""",
    """CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
        INSERT INTO entries_fts(entries_fts, rowid, ocr_text, window_title)
            VALUES ('delete', old.id, old.ocr_text, old.window_title);
    END""",
    # Phase 2: Kalendertermine (Outlook) und Anrufe (Teams) als gemeinsame, zeitgebundene Ereignisse
    """CREATE TABLE IF NOT EXISTS calendar_events(
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,                 -- 'outlook' | 'teams'
        ext_id TEXT,                          -- EntryID / callRecord-Id (Herkunfts-ID)
        ts_start INTEGER NOT NULL,
        ts_end INTEGER NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        location TEXT,
        organizer TEXT,
        attendees TEXT,                       -- '; '-getrennte Teilnehmerliste
        category TEXT,                        -- 'meeting' | 'appointment' | 'call'
        extra TEXT,                           -- JSON mit quellenspezifischen Feldern
        synced_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_events_ts_start ON calendar_events(ts_start)",
    "CREATE INDEX IF NOT EXISTS ix_events_source ON calendar_events(source, ts_start)",
    # Phase 2 (v3): Klassifizierungs-Cache fuer Teams-callRecords. Ein Einzelabruf je Anruf (Teilnehmer)
    # ist teuer; da callRecords unveraenderlich sind, wird das Ergebnis (bin ich beteiligt?) hier
    # persistiert, damit nicht bei jedem Sync erneut abgefragt wird.
    """CREATE TABLE IF NOT EXISTS teams_seen(
        ext_id TEXT PRIMARY KEY,
        involves_me INTEGER NOT NULL,         -- 1 = Nutzer war Organisator/Teilnehmer, 0 = nicht
        ts_start INTEGER NOT NULL,            -- fuer die Retention-Bereinigung
        seen_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_teams_seen_ts ON teams_seen(ts_start)",
)

ENTRY_COLUMNS = (
    "id, ts_start, ts_end, monitor_id, process_name, window_title, exe_path, "
    "width, height, ocr_status, ocr_conf, created_at"
)

_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')
_OPERATORS = {"OR", "AND", "NOT"}


def build_fts_query(text: str | None) -> str | None:
    """Uebersetzt eine Nutzereingabe in eine FTS5-MATCH-Abfrage.

    Unquotierte Woerter werden zu Praefix-Suchen ("wort"*), damit "Rechnung" auch
    "Rechnungsnummer" findet; Phrasen in Anfuehrungszeichen bleiben exakt (als Phrase).
    OR/AND/NOT werden als Operatoren durchgereicht. Leer -> None.
    """
    if not text:
        return None
    parts: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        phrase, word = m.group(1), m.group(2)
        if phrase is not None:
            p = phrase.strip().replace('"', "")
            if p:
                parts.append(f'"{p}"')
            continue
        assert word is not None
        if word.upper() in _OPERATORS:
            parts.append(word.upper())
            continue
        w = word.replace('"', "").strip("*")
        if not w:
            continue
        parts.append(f'"{w}"*')
    while parts and parts[0] in _OPERATORS:
        parts.pop(0)
    while parts and parts[-1] in _OPERATORS:
        parts.pop()
    return " ".join(parts) or None


class StorageClosed(Exception):
    """Zugriff auf eine bereits geschlossene Verbindung (z. B. Worker-Thread nach dem Herunterfahren)."""


class _Queries:
    """Lesemethoden, gemeinsam fuer Writer (Storage) und Reader (ReadOnlyStorage)."""

    _con: sqlcipher3.Connection
    _lock: threading.RLock
    db_path: Path
    _closed: bool = False

    # ---- Hilfsfunktionen -------------------------------------------------
    def _rows(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._con.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
        return [dict(r) for r in rows]

    def _one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _scalar(self, sql: str, params: tuple | dict = (), default: Any = None) -> Any:
        with self._lock:
            cur = self._con.execute(sql, params)
            row = cur.fetchone()
            cur.close()
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # ---- Einzelne Eintraege ----------------------------------------------
    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return self._one(f"SELECT {ENTRY_COLUMNS}, ocr_text, ocr_attempts FROM entries WHERE id = ?", (entry_id,))

    def get_image(self, entry_id: int) -> bytes | None:
        value = self._scalar("SELECT webp FROM images WHERE entry_id = ?", (entry_id,))
        return bytes(value) if value is not None else None

    def neighbor_ids(self, entry_id: int) -> tuple[int | None, int | None]:
        """(vorheriger, naechster) Eintrag nach ts_start."""
        row = self.get_entry(entry_id)
        if row is None:
            return None, None
        prev_id = self._scalar(
            "SELECT id FROM entries WHERE (ts_start < ? OR (ts_start = ? AND id < ?)) ORDER BY ts_start DESC, id DESC LIMIT 1",
            (row["ts_start"], row["ts_start"], entry_id))
        next_id = self._scalar(
            "SELECT id FROM entries WHERE (ts_start > ? OR (ts_start = ? AND id > ?)) ORDER BY ts_start ASC, id ASC LIMIT 1",
            (row["ts_start"], row["ts_start"], entry_id))
        return prev_id, next_id

    # ---- Zeitraeume ------------------------------------------------------
    def entries_between(self, start_ms: int, end_ms: int, *, with_text: bool = False,
                        max_span_ms: int = DEFAULT_MAX_SPAN_MS) -> list[dict[str, Any]]:
        """Alle Eintraege, die den Zeitraum [start, end) ueberlappen, chronologisch."""
        cols = ENTRY_COLUMNS + (", ocr_text" if with_text else "")
        return self._rows(
            f"SELECT {cols} FROM entries WHERE ts_start >= ? AND ts_start < ? AND ts_end >= ? "
            "ORDER BY ts_start, monitor_id, id",
            (start_ms - max_span_ms, end_ms, start_ms))

    def entries_around(self, ts_ms: int, window_ms: int, *, with_text: bool = True) -> list[dict[str, Any]]:
        return self.entries_between(ts_ms - window_ms, ts_ms + window_ms + 1, with_text=with_text)

    def count_between(self, start_ms: int, end_ms: int) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM entries WHERE ts_start >= ? AND ts_start < ?",
                                (start_ms, end_ms), 0))

    # ---- Kalender-/Anruf-Ereignisse (Phase 2) ----------------------------
    def events_between(self, start_ms: int, end_ms: int, *, sources: list[str] | None = None) -> list[dict[str, Any]]:
        """Ereignisse (Outlook-Termine, Teams-Anrufe), die den Zeitraum [start, end) ueberlappen."""
        where = ["ts_start < ?", "ts_end >= ?"]
        params: list[Any] = [end_ms, start_ms]
        if sources:
            where.append("source IN (%s)" % ",".join("?" * len(sources)))
            params.extend(sources)
        return self._rows(
            f"SELECT {EVENT_COLUMNS} FROM calendar_events WHERE {' AND '.join(where)} ORDER BY ts_start, ts_end",
            tuple(params))

    def events_around(self, ts_ms: int, window_ms: int, *, sources: list[str] | None = None) -> list[dict[str, Any]]:
        return self.events_between(ts_ms - window_ms, ts_ms + window_ms + 1, sources=sources)

    def event_bounds(self) -> tuple[int | None, int | None]:
        row = self._one("SELECT MIN(ts_start) AS oldest, MAX(ts_end) AS newest FROM calendar_events")
        return (row["oldest"], row["newest"]) if row else (None, None)

    def event_by_ext_id(self, source: str, ext_id: str, ts_start: int) -> dict[str, Any] | None:
        """Ein Ereignis ueber Quelle, Herkunfts-Id und Beginn (der Beginn nutzt den Index source+ts_start)."""
        return self._one(f"SELECT {EVENT_COLUMNS} FROM calendar_events WHERE source = ? AND ts_start = ? "
                         "AND ext_id = ?", (source, int(ts_start), ext_id))

    def teams_seen_map(self, start_ms: int | None = None, end_ms: int | None = None) -> dict[str, bool]:
        """ext_id -> involves_me fuer bereits klassifizierte Teams-Anrufe (Cache, optional zeitlich begrenzt)."""
        where: list[str] = []
        params: list[Any] = []
        if start_ms is not None:
            where.append("ts_start >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            where.append("ts_start < ?")
            params.append(int(end_ms))
        sql = "SELECT ext_id, involves_me FROM teams_seen" + (" WHERE " + " AND ".join(where) if where else "")
        return {r["ext_id"]: bool(r["involves_me"]) for r in self._rows(sql, tuple(params))}

    # ---- Suche -----------------------------------------------------------
    def search(self, query: str, *, from_ms: int | None = None, to_ms: int | None = None,
               limit: int = 20, order: str = "rank") -> list[dict[str, Any]]:
        """FTS5-Suche ueber OCR-Text und Fenstertitel. order: 'rank' (Relevanz) oder 'time' (neueste zuerst)."""
        match = build_fts_query(query)
        if not match:
            return []
        limit = max(1, min(int(limit), 500))
        where = ["entries_fts MATCH ?"]
        params: list[Any] = [match]
        if from_ms is not None:
            where.append("e.ts_start >= ?")
            params.append(int(from_ms))
        if to_ms is not None:
            where.append("e.ts_start < ?")
            params.append(int(to_ms))
        # Alias bewusst NICHT "rank": so heisst die versteckte FTS5-Spalte, die den Alias ueberdecken wuerde
        order_sql = "e.ts_start DESC" if order == "time" else "score, e.ts_start DESC"
        params.append(limit)
        sql = (
            "SELECT e.id, e.ts_start, e.ts_end, e.monitor_id, e.process_name, e.window_title, e.ocr_status, "
            # Eindeutige Steuerzeichen (STX/ETX) als Treffer-Marker statt [ ], damit eckige Klammern im
            # OCR-Text (z. B. [INFO], array[0]) die Hervorhebung nicht zerreissen. UI/MCP wandeln sie zurueck.
            "snippet(entries_fts, 0, char(2), char(3), ' … ', 14) AS snippet, "
            "bm25(entries_fts, 1.0, 3.0) AS score "
            "FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
            f"WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ?"
        )
        try:
            return self._rows(sql, tuple(params))
        except sqlcipher3.OperationalError as e:  # ungueltige FTS-Syntax
            log.debug("FTS-Abfrage ungueltig (%s): %s", match, e)
            return []

    # ---- Statistiken -----------------------------------------------------
    def app_stats(self, start_ms: int, end_ms: int, *, max_span_ms: int = DEFAULT_MAX_SPAN_MS) -> list[dict[str, Any]]:
        """Je Prozess: Anzahl Eintraege, kumulierte Dauer (auf den Zeitraum beschnitten), erstes/letztes Auftreten."""
        return self._rows(
            "SELECT process_name, COUNT(*) AS entries, "
            "SUM(MAX(0, MIN(ts_end, :end) - MAX(ts_start, :start))) AS total_ms, "
            # first_seen/last_seen auf den angefragten Zeitraum beschneiden (sonst zieht die max_span-Untergrenze
            # in den Vortag hineinreichende Eintraege mit falschen Randzeiten herein)
            "MIN(MAX(ts_start, :start)) AS first_ms, MAX(MIN(ts_end, :end)) AS last_ms, "
            "COUNT(DISTINCT window_title) AS titles "
            "FROM entries WHERE ts_start >= :lower AND ts_start < :end AND ts_end >= :start "
            "GROUP BY process_name ORDER BY total_ms DESC, entries DESC",
            {"start": start_ms, "end": end_ms, "lower": start_ms - max_span_ms})

    def title_stats(self, start_ms: int, end_ms: int, process_name: str, limit: int = 10,
                    *, max_span_ms: int = DEFAULT_MAX_SPAN_MS) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT window_title, COUNT(*) AS entries, "
            "SUM(MAX(0, MIN(ts_end, :end) - MAX(ts_start, :start))) AS total_ms, "
            "MIN(MAX(ts_start, :start)) AS first_ms, MAX(MIN(ts_end, :end)) AS last_ms "
            "FROM entries WHERE process_name = :proc AND ts_start >= :lower AND ts_start < :end AND ts_end >= :start "
            "GROUP BY window_title ORDER BY total_ms DESC LIMIT :limit",
            {"start": start_ms, "end": end_ms, "lower": start_ms - max_span_ms, "proc": process_name, "limit": limit})

    def list_days(self) -> list[str]:
        """Lokale Kalendertage (ISO-Datum) mit mindestens einem Eintrag, aufsteigend."""
        offset = timeutil.local_offset_ms()
        rows = self._rows(
            "SELECT DISTINCT (ts_start + ?) / 86400000 AS d FROM entries ORDER BY d", (offset,))
        return [datetime.fromtimestamp(int(r["d"]) * 86400, tz=timezone.utc).date().isoformat() for r in rows]

    def pending_ocr_ids(self, limit: int = 50) -> list[int]:
        rows = self._rows("SELECT id FROM entries WHERE ocr_status = 0 ORDER BY id DESC LIMIT ?", (limit,))
        return [int(r["id"]) for r in rows]

    def bounds(self) -> tuple[int | None, int | None]:
        row = self._one("SELECT MIN(ts_start) AS oldest, MAX(ts_end) AS newest FROM entries")
        if not row:
            return None, None
        return row["oldest"], row["newest"]

    def db_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def stats(self) -> dict[str, Any]:
        oldest, newest = self.bounds()
        return {
            "entries": int(self._scalar("SELECT COUNT(*) FROM entries", (), 0)),
            "pending_ocr": int(self._scalar("SELECT COUNT(*) FROM entries WHERE ocr_status = 0", (), 0)),
            "oldest_ms": oldest,
            "newest_ms": newest,
            "page_count": int(self._scalar("PRAGMA page_count", (), 0)),
            "page_size": int(self._scalar("PRAGMA page_size", (), 0)),
            "freelist_count": int(self._scalar("PRAGMA freelist_count", (), 0)),
            "db_size_bytes": self.db_size_bytes(),
        }

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        value = self._scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return default if value is None else str(value)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            try:
                self._con.close()
            except sqlcipher3.Error:  # pragma: no cover
                log.debug("close fehlgeschlagen", exc_info=True)


class Storage(_Queries):
    """Schreibende Verbindung (genau eine pro Dienstprozess), threadsicher ueber RLock."""

    def __init__(self, db_path: Path, key: bytes):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._con = open_database(self.db_path, key, read_only=False)
        self._init_schema()

    # ---- Transaktionen ---------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlcipher3.Connection]:
        with self._lock:
            if self._closed:
                # Worker-Thread schreibt nach dem Herunterfahren (z. B. Sync kehrt aus einem HTTP-Aufruf
                # zurueck, nachdem shutdown() die Verbindung geschlossen hat) -> sauber abweisen statt
                # use-after-close. Die Worker-Schleifen fangen das ab.
                raise StorageClosed(self.db_path.name)
            self._con.execute("BEGIN IMMEDIATE")
            try:
                yield self._con
            except BaseException:
                try:
                    self._con.execute("ROLLBACK")
                except sqlcipher3.Error:
                    # Schlaegt das ROLLBACK selbst fehl, darf die geteilte Verbindung nicht mit offener
                    # Transaktion zurueckbleiben (sonst wedgen alle folgenden BEGIN IMMEDIATE).
                    log.warning("ROLLBACK fehlgeschlagen", exc_info=True)
                raise
            else:
                self._con.execute("COMMIT")

    def _init_schema(self) -> None:
        with self.transaction() as con:
            for stmt in SCHEMA_SQL:
                con.execute(stmt)
            # Fehlende Spalten nachziehen: CREATE TABLE IF NOT EXISTS aendert eine bestehende Tabelle nicht.
            spalten = {row[1] for row in con.execute("PRAGMA table_info(entries)").fetchall()}
            if "ocr_attempts" not in spalten:
                con.execute("ALTER TABLE entries ADD COLUMN ocr_attempts INTEGER NOT NULL DEFAULT 0")
                log.info("Spalte entries.ocr_attempts ergaenzt")
            version = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if version is None:
                con.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
                con.execute("INSERT INTO meta(key, value) VALUES ('created_at', ?)", (str(timeutil.now_ms()),))
            elif int(version[0]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Datenbank-Schema {version[0]} ist neuer als diese Programmversion ({SCHEMA_VERSION})")
            elif int(version[0]) < SCHEMA_VERSION:
                # Migration: neue Tabellen wurden oben per IF NOT EXISTS bereits angelegt
                con.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))
                log.info("Datenbank-Schema von v%s auf v%s migriert", version[0], SCHEMA_VERSION)

    # ---- Schreiben -------------------------------------------------------
    def insert_entry(self, *, ts_start: int, ts_end: int, monitor_id: int, process_name: str,
                     window_title: str, exe_path: str | None, width: int, height: int, webp: bytes) -> int:
        with self.transaction() as con:
            cur = con.execute(
                "INSERT INTO entries(ts_start, ts_end, monitor_id, process_name, window_title, exe_path, "
                "width, height, ocr_status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (int(ts_start), int(ts_end), int(monitor_id), process_name or "", window_title or "",
                 exe_path, int(width), int(height), OCR_PENDING, timeutil.now_ms()))
            entry_id = int(cur.lastrowid)
            con.execute("INSERT INTO images(entry_id, webp) VALUES (?, ?)", (entry_id, sqlcipher3.Binary(webp)))
            return entry_id

    def extend_entries(self, updates: dict[int, int]) -> None:
        """Verlaengert ts_end mehrerer Eintraege (Puffer-Flush aus der Aufnahme)."""
        if not updates:
            return
        with self.transaction() as con:
            con.executemany("UPDATE entries SET ts_end = MAX(ts_end, ?) WHERE id = ?",
                            [(int(ts), int(eid)) for eid, ts in updates.items()])

    def extend_entry(self, entry_id: int, ts_end: int) -> None:
        self.extend_entries({entry_id: ts_end})

    def set_ocr_result(self, entry_id: int, text: str | None, conf: float | None, status: int = OCR_DONE) -> None:
        with self.transaction() as con:
            con.execute("UPDATE entries SET ocr_text = ?, ocr_conf = ?, ocr_status = ? WHERE id = ?",
                        (text, conf, int(status), int(entry_id)))

    def mark_ocr_failed(self, entry_id: int) -> None:
        with self.transaction() as con:
            con.execute("UPDATE entries SET ocr_status = ?, ocr_attempts = ocr_attempts + 1 "
                        "WHERE id = ? AND ocr_status = ?",
                        (OCR_FAILED, int(entry_id), OCR_PENDING))

    def reset_failed_ocr(self, max_attempts: int = MAX_OCR_ATTEMPTS) -> int:
        """Setzt gescheiterte Eintraege wieder auf offen - aber nur begrenzt oft.

        Ohne Grenze wandert ein Bild, an dem die Texterkennung jedes Mal scheitert, bei jedem
        Wartungslauf zurueck in die Warteschlange und kostet dauerhaft Rechenzeit, ohne je ein
        Ergebnis zu liefern.
        """
        with self.transaction() as con:
            return con.execute("UPDATE entries SET ocr_status = ? WHERE ocr_status = ? AND ocr_attempts < ?",
                               (OCR_PENDING, OCR_FAILED, int(max_attempts))).rowcount

    # ---- Kalender-/Anruf-Ereignisse schreiben ----------------------------
    def replace_events(self, source: str, window_start: int, window_end: int,
                       events: list[dict[str, Any]]) -> int:
        """Ersetzt alle Ereignisse einer Quelle, die das Fenster [start, end) ueberlappen, durch `events`.

        So bleibt eine erneute Synchronisierung idempotent (geloeschte/verschobene Termine verschwinden),
        ohne dass Ereignisse ausserhalb des synchronisierten Fensters angetastet werden.
        """
        now = timeutil.now_ms()
        with self.transaction() as con:
            con.execute("DELETE FROM calendar_events WHERE source = ? AND ts_start < ? AND ts_end >= ?",
                        (source, int(window_end), int(window_start)))
            for e in events:
                con.execute(
                    "INSERT INTO calendar_events(source, ext_id, ts_start, ts_end, subject, location, organizer, "
                    "attendees, category, extra, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (source, e.get("ext_id"), int(e["ts_start"]), int(e["ts_end"]), e.get("subject") or "",
                     e.get("location"), e.get("organizer"), e.get("attendees"), e.get("category"),
                     e.get("extra"), now))
        return len(events)

    def upsert_event(self, source: str, e: dict[str, Any]) -> None:
        """Ein Ereignis anlegen oder - gleiche Quelle, Herkunfts-Id und gleicher Beginn - fortschreiben.

        Fuer beobachtende Plugins: Ein laufendes Gespraech wird bei jedem Blick verlaengert, statt als
        Kette einzelner Zeilen zu landen.
        """
        now = timeutil.now_ms()
        with self.transaction() as con:
            cur = con.execute(
                "UPDATE calendar_events SET ts_end = ?, subject = ?, location = ?, organizer = ?, attendees = ?, "
                "category = ?, extra = ?, synced_at = ? WHERE source = ? AND ts_start = ? AND ext_id = ?",
                (int(e["ts_end"]), e.get("subject") or "", e.get("location"), e.get("organizer"),
                 e.get("attendees"), e.get("category"), e.get("extra"), now,
                 source, int(e["ts_start"]), e["ext_id"]))
            if cur.rowcount == 0:
                con.execute(
                    "INSERT INTO calendar_events(source, ext_id, ts_start, ts_end, subject, location, organizer, "
                    "attendees, category, extra, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (source, e["ext_id"], int(e["ts_start"]), int(e["ts_end"]), e.get("subject") or "",
                     e.get("location"), e.get("organizer"), e.get("attendees"), e.get("category"),
                     e.get("extra"), now))

    def delete_events_older_than(self, cutoff_ms: int, batch: int = 500) -> int:
        with self.transaction() as con:
            return con.execute(
                "DELETE FROM calendar_events WHERE id IN "
                "(SELECT id FROM calendar_events WHERE ts_start < ? ORDER BY ts_start LIMIT ?)",
                (int(cutoff_ms), int(batch))).rowcount

    def teams_mark_seen(self, entries: list[tuple[str, bool, int]]) -> None:
        """Merkt die Klassifizierung von Teams-Anrufen (ext_id, involves_me, ts_start) im Cache."""
        if not entries:
            return
        now = timeutil.now_ms()
        with self.transaction() as con:
            con.executemany(
                "INSERT INTO teams_seen(ext_id, involves_me, ts_start, seen_at) VALUES (?,?,?,?) "
                "ON CONFLICT(ext_id) DO UPDATE SET involves_me=excluded.involves_me, seen_at=excluded.seen_at",
                [(str(eid), 1 if inv else 0, int(ts), now) for eid, inv, ts in entries if eid])

    def delete_teams_seen_older_than(self, cutoff_ms: int, batch: int = 500) -> int:
        with self.transaction() as con:
            return con.execute(
                "DELETE FROM teams_seen WHERE ext_id IN "
                "(SELECT ext_id FROM teams_seen WHERE ts_start < ? ORDER BY ts_start LIMIT ?)",
                (int(cutoff_ms), int(batch))).rowcount

    def delete_events_of_source(self, source: str) -> int:
        """Alle Ereignisse einer Quelle - beim Entfernen des zugehoerigen Plugins."""
        with self.transaction() as con:
            return con.execute("DELETE FROM calendar_events WHERE source = ?", (source,)).rowcount

    def teams_clear_seen(self) -> int:
        with self.transaction() as con:
            return con.execute("DELETE FROM teams_seen").rowcount

    def delete_entry(self, entry_id: int) -> int:
        with self.transaction() as con:
            return con.execute("DELETE FROM entries WHERE id = ?", (int(entry_id),)).rowcount

    def delete_between(self, from_ms: int, to_ms: int) -> int:
        """Loescht Eintraege, deren Aufnahme im Zeitraum [from, to) begann oder noch lief."""
        with self.transaction() as con:
            return con.execute(
                "DELETE FROM entries WHERE (ts_start >= ? AND ts_start < ?) OR (ts_start < ? AND ts_end >= ?)",
                (int(from_ms), int(to_ms), int(from_ms), int(from_ms))).rowcount

    def delete_older_than(self, cutoff_ms: int, batch: int = 200) -> int:
        """Loescht einen Batch der aeltesten Eintraege vor cutoff; Rueckgabe: geloeschte Zeilen."""
        with self.transaction() as con:
            return con.execute(
                "DELETE FROM entries WHERE id IN (SELECT id FROM entries WHERE ts_start < ? ORDER BY ts_start LIMIT ?)",
                (int(cutoff_ms), int(batch))).rowcount

    def delete_oldest(self, count: int) -> int:
        with self.transaction() as con:
            return con.execute(
                "DELETE FROM entries WHERE id IN (SELECT id FROM entries ORDER BY ts_start LIMIT ?)",
                (int(count),)).rowcount

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as con:
            con.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, str(value)))

    # ---- Wartung ---------------------------------------------------------
    def backup_to(self, target) -> int:
        """Konsistente, ebenfalls verschluesselte Kopie per VACUUM INTO. Gibt die Groesse in Bytes zurueck.

        VACUUM INTO schreibt einen sauberen Stand auch waehrend laufender Schreibzugriffe - anders als ein
        blosses Kopieren der Datei, das mitten in einer Transaktion eine unbrauchbare Kopie ergaebe.
        Die Kopie traegt denselben Schluessel wie das Original.
        """
        import os
        from pathlib import Path as _Path

        target = _Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        if tmp.exists():
            tmp.unlink()
        with self._lock:
            if self._closed:
                raise StorageClosed("Datenbank ist geschlossen")
            self._con.execute("VACUUM INTO ?", (str(tmp),))
        os.replace(tmp, target)
        return target.stat().st_size

    def incremental_vacuum(self, pages: int | None = None) -> int:
        """Gibt Freelist-Seiten an das Dateisystem zurueck. Rueckgabe: Anzahl freigegebener Seiten.
        Die PRAGMA-Anweisung arbeitet schrittweise und muss vollstaendig iteriert werden."""
        sql = "PRAGMA incremental_vacuum" + (f"({int(pages)})" if pages else "")
        with self._lock:
            before = self._con.execute("PRAGMA freelist_count").fetchone()[0]
            self._con.execute(sql).fetchall()   # PRAGMA liefert keine Zeilen; muss vollstaendig iteriert werden
            after = self._con.execute("PRAGMA freelist_count").fetchone()[0]
        return max(0, int(before) - int(after))

    def checkpoint(self, mode: str = "TRUNCATE") -> tuple[int, int, int]:
        if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            raise ValueError(mode)
        with self._lock:
            cur = self._con.execute(f"PRAGMA wal_checkpoint({mode})")
            row = cur.fetchone()
            cur.close()
        return tuple(int(v) for v in row)  # (busy, log_pages, checkpointed)

    def optimize_fts(self) -> None:
        with self._lock:
            self._con.execute("INSERT INTO entries_fts(entries_fts) VALUES ('optimize')")

    def vacuum_full(self) -> None:
        """Vollstaendiges VACUUM. Laeuft mit temp_store=MEMORY, benoetigt also RAM in DB-Groesse."""
        with self._lock:
            self._con.execute("VACUUM")

    def interrupt(self) -> None:
        """Bricht laufende Anweisungen ab (z. B. VACUUM beim Beenden)."""
        try:
            self._con.interrupt()
        except sqlcipher3.Error:  # pragma: no cover
            pass

    def close(self) -> None:
        with self._lock:
            try:
                self._con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
            except sqlcipher3.Error:
                log.debug("Checkpoint beim Schliessen fehlgeschlagen", exc_info=True)
            super().close()


class ReadOnlyStorage(_Queries):
    """Nur-Lese-Verbindung (Timeline-Bridge, MCP-Server)."""

    def __init__(self, db_path: Path, key: bytes):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._con = open_database(self.db_path, key, read_only=True, create=False)
