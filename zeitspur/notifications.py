"""Windows-Benachrichtigungen: Mitteilungen von Teams, Outlook, Slack, WhatsApp, Signal ...

Gelesen aus der Benachrichtigungs-Datenbank von Windows - nur lesend, direkt aus der Datei:
    %LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\wpndatabase.db
    Notification (Id, HandlerId, Type, Payload = Toast-XML, ArrivalTime = FILETIME)
    NotificationHandler (RecordId, PrimaryId = App-Kennung, etwa "MSTeams_8wekyb3d8bbwe!MSTeams")
Windows behaelt dort nur die aktuellen Mitteilungen; deshalb schaut Zeitspur regelmaessig nach und schreibt neue
selbst mit. Rueckwirkend gibt es nichts.

Heikel: Mitteilungen enthalten oft Nachrichten anderer Menschen. Gespeichert wird verschluesselt wie alles andere;
die Textvorschau laesst sich abschalten, Windows-Systemmeldungen bleiben standardmaessig aussen vor. Solange die
Aufnahme pausiert, wird nichts gelesen.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .teams_local import filetime_to_ms

log = logging.getLogger(__name__)

SOURCE = "notifications"
POLL_MS = 30_000
MAX_TITLE = 160
MAX_TEXT = 500
_SEEN_LIMIT = 5000

# App-Kennung (Teil vor '!' und vor '_', klein) -> Anzeigename
APP_NAMES = {
    "msteams": "Teams", "microsoft.outlookforwindows": "Outlook", "microsoft.office.outlook.exe.15": "Outlook",
    "5319275a.whatsappdesktop": "WhatsApp", "com.squirrel.slack.slack": "Slack", "91750d7e.slack": "Slack",
    "claude": "Claude", "microsoft.skypeapp": "Skype", "telegramdesktop": "Telegram",
    "org.whispersystems.signal-desktop": "Signal", "com.squirrel.discord.discord": "Discord",
    "zoom.us": "Zoom", "microsoft.todos": "To Do", "microsoft.windowscommunicationsapps": "Mail",
    "microsoft.yourphone": "Smartphone-Link", "microsoftcorporationii.windowsapps": "Windows",
}
_SYSTEM_PREFIXES = ("windows.", "microsoft.windows.", "microsoft.windowsstore", "microsoft.xboxgamingoverlay",
                    "microsoft.security", "fortinet", "forticlient", "microsoft.onedrive")


def db_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / \
        "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"


@dataclass(frozen=True)
class Toast:
    id: int
    arrival_ms: int
    app_id: str
    texts: tuple[str, ...]


def parse_toast_texts(payload) -> tuple[str, ...]:
    """Toast-XML -> sichtbare Texte in Reihenfolge (Titel zuerst)."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return ()
    texts = []
    for node in root.iter("text"):
        value = " ".join((node.text or "").split())
        if value:
            texts.append(value)
    return tuple(texts)


def app_name(app_id: str) -> tuple[str, bool]:
    """App-Kennung -> (Anzeigename, Systemmeldung?)."""
    base = app_id.split("!", 1)[0]
    family = base.split("_", 1)[0].lower()
    system = family.startswith(_SYSTEM_PREFIXES)
    name = APP_NAMES.get(family) or APP_NAMES.get(base.lower())
    if not name:
        name = base.split("_", 1)[0].rsplit(".", 1)[-1] or base
    return name, system


def access_reason(path: Path | None = None) -> str | None:
    if sys.platform != "win32":
        return "Nur unter Windows verfügbar."
    p = path or db_path()
    if not p.exists():
        return "Windows speichert auf diesem PC keine Benachrichtigungen."
    return None


def read_toasts(path: Path | None = None) -> list[Toast]:
    p = path or db_path()
    con = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        rows = con.execute(
            "SELECT n.Id, n.ArrivalTime, h.PrimaryId, n.Payload FROM Notification n "
            "JOIN NotificationHandler h ON h.RecordId = n.HandlerId WHERE n.Type = 'toast'").fetchall()
    finally:
        con.close()
    out = []
    for nid, arrival, app_id, payload in rows:
        if not arrival:
            continue
        out.append(Toast(int(nid), filetime_to_ms(int(arrival)), str(app_id or ""), parse_toast_texts(payload)))
    return sorted(out, key=lambda t: t.arrival_ms)


def build_row(toast: Toast, name: str, *, store_text: bool) -> dict:
    title = toast.texts[0] if toast.texts else "(ohne Titel)"
    extra = {"app": name}
    if store_text and len(toast.texts) > 1:
        extra["text"] = " · ".join(toast.texts[1:])[:MAX_TEXT]
    return {"ext_id": f"wpn-{toast.id}-{toast.arrival_ms}", "ts_start": toast.arrival_ms, "ts_end": toast.arrival_ms,
            "subject": f"{name}: {title}"[:MAX_TITLE], "location": name, "organizer": None, "attendees": None,
            "category": "notification", "extra": json.dumps(extra, ensure_ascii=False)}


class NotificationRecorder:
    """Schreibt neue Mitteilungen mit. Was schon gespeichert ist (auch vor einem Neustart), bleibt einmalig."""

    def __init__(self, read=read_toasts):
        self._read = read
        self._last_poll = -POLL_MS
        self._seen: set[str] = set()

    def observe(self, storage, now_ms: int, settings: dict, *, titles_allowed: bool = True) -> int:
        if not titles_allowed:
            return 0          # Aufnahme pausiert: keine Inhalte lesen
        if now_ms - self._last_poll < POLL_MS:
            return 0
        self._last_poll = now_ms
        ignore = {a.strip().lower() for a in settings.get("ignore_apps") or [] if a.strip()}
        only = {a.strip().lower() for a in settings.get("only_apps") or [] if a.strip()}
        if len(self._seen) > _SEEN_LIMIT:
            self._seen.clear()
        written = 0
        for toast in self._read():
            row_id = f"wpn-{toast.id}-{toast.arrival_ms}"
            if row_id in self._seen:
                continue
            self._seen.add(row_id)
            name, system = app_name(toast.app_id)
            if system and settings.get("ignore_system", True):
                continue
            if name.lower() in ignore or (only and name.lower() not in only):
                continue
            if storage.event_by_ext_id(SOURCE, row_id, toast.arrival_ms):
                continue
            storage.upsert_event(SOURCE, build_row(toast, name, store_text=bool(settings.get("store_text", True))))
            written += 1
        return written


def describe_current(read=read_toasts) -> str:
    """Fuer "Verbindung testen": wie viele Mitteilungen Windows gerade vorhaelt, von welchen Apps."""
    try:
        toasts = read()
    except sqlite3.Error as e:
        return f"Benachrichtigungen nicht lesbar: {e}"
    if not toasts:
        return "Bereit. Windows hält gerade keine Mitteilungen vor – neue werden ab jetzt erfasst."
    apps = sorted({app_name(t.app_id)[0] for t in toasts})
    newest = datetime.fromtimestamp(toasts[-1].arrival_ms / 1000).strftime("%d.%m. %H:%M")
    return (f"Bereit. Windows hält {len(toasts)} Mitteilungen vor (neueste {newest}) von: "
            + ", ".join(apps[:8]) + ".")
