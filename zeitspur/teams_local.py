"""Teams-Gespraeche ohne Entra-App: erkannt daran, dass Teams das Mikrofon benutzt.

Microsoft gibt die Anrufliste (Graph callRecords) nur an eine registrierte App mit Admin-Zustimmung heraus;
eine delegierte Berechtigung gibt es dafuer nicht. Windows protokolliert aber fuer jede App, wann sie das
Mikrofon benutzt - dieselbe Quelle, aus der das Mikrofon-Symbol in der Taskleiste gespeist wird:

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone
        \\MSTeams_8wekyb3d8bbwe            (neues Teams, als App-Paket)
        \\NonPackaged\\C:#...#Teams.exe     (klassisches Teams)
    LastUsedTimeStart / LastUsedTimeStop  (REG_QWORD, FILETIME in UTC; Stop = 0 waehrend der Nutzung)

Gegengeprueft mit Microsoft Graph: Die Mikrofon-Zeiten eines ausgehenden, nicht angenommenen Anrufs stimmten
auf die Sekunde mit dem Anrufprotokoll ueberein. Windows haelt nur die LETZTE Nutzung je App fest - deshalb fragt Zeitspur
alle paar Sekunden nach und schreibt jedes Gespraech selbst mit. Rueckwirkend gibt es nichts.

Namen und Nummern liefert das Mikrofon nicht. Waehrend des Gespraechs werden deshalb die Fenstertitel von
Teams mitgelesen (alle Fenster, nicht nur das vorderste); zeigt Teams dort "Anruf von ..." oder ein
eigenes Besprechungsfenster, wird das als Gegenueber uebernommen. Mehr wird nicht geraten.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

SOURCE = "teams_local"
CONSENT_STORE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
PACKAGED_TEAMS = ("MSTeams_8wekyb3d8bbwe",)
TEAMS_PROCESSES = frozenset({"ms-teams.exe", "teams.exe"})
FILETIME_UNIX_EPOCH = 116_444_736_000_000_000   # 1970-01-01 in 100-ns-Schritten seit 1601
MIN_SESSION_MS = 5_000       # kuerzere Mikrofon-Nutzung (Geraetetest, Fehlklick) ist kein Gespraech
MAX_TITLES = 5

# Fenstertitel, die Teams immer zeigt - ohne Aussage ueber das Gegenueber
GENERIC_TITLES = frozenset({
    "", "chat", "chats", "anrufe", "calls", "aktivität", "activity", "kalender", "calendar", "teams",
    "microsoft teams", "dateien", "files", "apps", "kompakte besprechungsansicht", "compact meeting view",
    "benachrichtigungen", "notifications", "einstellungen", "settings", "suche", "search", "copilot",
    "onedrive", "communities", "personen", "people", "assignments", "aufgaben",
})
CALL_TITLE = re.compile(r"^(Anruf|Telefonat|Call)\b", re.IGNORECASE)


@dataclass(frozen=True)
class MicUsage:
    """Letzte Mikrofon-Nutzung einer App, wie Windows sie festhaelt."""
    app: str
    start_ms: int
    stop_ms: int | None          # None = Mikrofon ist gerade in Benutzung

    @property
    def running(self) -> bool:
        return self.stop_ms is None


def filetime_to_ms(ft: int) -> int:
    return (int(ft) - FILETIME_UNIX_EPOCH) // 10_000


def _usage_from_values(app: str, start_ft: int | None, stop_ft: int | None) -> MicUsage | None:
    if not start_ft:
        return None
    start = filetime_to_ms(start_ft)
    # Waehrend der Nutzung steht Stop auf 0. Steht es vor dem Start, laeuft eine neue Nutzung.
    stop = None if not stop_ft or stop_ft < start_ft else filetime_to_ms(stop_ft)
    return MicUsage(app=app, start_ms=start, stop_ms=stop)


def mic_access_reason() -> str | None:
    """Grund, warum sich die Mikrofon-Nutzung nicht auslesen laesst - sonst None."""
    if sys.platform != "win32":
        return "Nur unter Windows verfügbar."
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONSENT_STORE) as key:
            value = winreg.QueryValueEx(key, "Value")[0]
    except OSError:
        return "Windows führt auf diesem PC kein Mikrofon-Protokoll."
    if str(value).lower() == "deny":
        return ("Der Mikrofonzugriff für Apps ist in den Windows-Datenschutzeinstellungen gesperrt - "
                "dann kann auch Teams kein Mikrofon benutzen.")
    return None


def read_mic_usage() -> list[MicUsage]:
    """Die letzte Mikrofon-Nutzung je installiertem Teams (neu und klassisch)."""
    if sys.platform != "win32":
        return []
    import winreg

    def lies(pfad: str, app: str) -> MicUsage | None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, pfad) as key:
                start = winreg.QueryValueEx(key, "LastUsedTimeStart")[0]
                stop = winreg.QueryValueEx(key, "LastUsedTimeStop")[0]
        except OSError:
            return None
        return _usage_from_values(app, start, stop)

    usages = [u for app in PACKAGED_TEAMS if (u := lies(rf"{CONSENT_STORE}\{app}", app))]
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONSENT_STORE + r"\NonPackaged") as key:
            namen = [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
    except OSError:
        namen = []
    for name in namen:  # Pfad mit '#' statt '\', z. B. C:#Users#...#Teams#current#Teams.exe
        if name.rsplit("#", 1)[-1].lower() in TEAMS_PROCESSES:
            if u := lies(rf"{CONSENT_STORE}\NonPackaged\{name}", "Teams (klassisch)"):
                usages.append(u)
    return usages


def teams_window_titles() -> list[str]:
    """Titel aller sichtbaren Teams-Fenster - auch der nicht vorne liegenden (dort laeuft oft der Anruf)."""
    if sys.platform != "win32":
        return []
    import psutil
    import win32gui
    import win32process

    titles: list[str] = []
    namen: dict[int, str] = {}

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        if pid not in namen:
            try:
                namen[pid] = psutil.Process(pid).name().lower()
            except Exception:
                namen[pid] = ""
        if namen[pid] in TEAMS_PROCESSES and title not in titles:
            titles.append(title)
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        log.debug("Teams-Fenster liessen sich nicht aufzaehlen", exc_info=True)
    return titles


def call_detail(titles: list[str]) -> str | None:
    """Das Gegenueber aus den Fenstertiteln - nur, wenn Teams es ausdruecklich zeigt."""
    erste = [t.split(" | ")[0].strip() for t in titles]
    for teil in erste:
        if CALL_TITLE.match(teil):               # "Anruf von +49 ... im Auftrag von Zentrale"
            return teil
    for titel, teil in zip(titles, erste):
        # Ein eigenes Besprechungsfenster heisst "<Betreff> | Microsoft Teams". Das Hauptfenster
        # traegt dagegen Bereich, Gegenueber, Organisation und Konto - es zeigt, was man ansieht,
        # nicht mit wem man spricht.
        if titel.count(" | ") == 1 and teil.lower() not in GENERIC_TITLES:
            return teil
    return None


def build_row(usage: MicUsage, end_ms: int, titles: list[str]) -> dict:
    detail = call_detail(titles)
    extra = {"in_progress": usage.running, "app": usage.app, "detected_by": "microphone"}
    if titles:
        extra["window_titles"] = titles[:MAX_TITLES]
    return {
        "ext_id": f"mic-{usage.start_ms}",
        "ts_start": usage.start_ms, "ts_end": max(end_ms, usage.start_ms),
        "subject": "Teams-Gespräch" + (f": {detail}" if detail else ""),
        "location": "Microsoft Teams", "organizer": None, "attendees": detail,
        "category": "call", "extra": json.dumps(extra, ensure_ascii=False),
    }


class CallRecorder:
    """Schreibt jede Teams-Mikrofon-Nutzung als Gespraech mit.

    Zustandslos ueber Neustarts hinweg: Was schon gespeichert ist, wird aus der Datenbank gelesen und
    ergaenzt. Ein laufendes Gespraech wird bei jedem Blick verlaengert (in_progress), ein beendetes
    einmal mit der genauen Endzeit abgeschlossen und danach nicht mehr angefasst.
    """

    def __init__(self, read_usage=read_mic_usage, read_titles=teams_window_titles):
        self._read_usage = read_usage
        self._read_titles = read_titles
        self._final: set[str] = set()

    def observe(self, storage, now_ms: int, *, titles_allowed: bool = True) -> int:
        """Ein Blick: liefert die Anzahl gespeicherter Zeilen (0, wenn sich nichts geaendert hat).
        titles_allowed=False (Aufnahme pausiert): nur die Zeiten, keine Fenstertitel."""
        written = 0
        for usage in self._read_usage():
            ext_id = f"mic-{usage.start_ms}"
            if ext_id in self._final:
                continue
            end = now_ms if usage.running else usage.stop_ms
            if end - usage.start_ms < MIN_SESSION_MS:
                continue
            stored = storage.event_by_ext_id(SOURCE, ext_id, usage.start_ms)
            stored_extra = json.loads(stored["extra"] or "{}") if stored else {}
            if stored and not usage.running and not stored_extra.get("in_progress"):
                self._final.add(ext_id)          # schon abgeschlossen gespeichert (z. B. vor einem Neustart)
                continue
            titles = list(stored_extra.get("window_titles") or [])
            if usage.running and titles_allowed:
                for t in self._read_titles():
                    if t not in titles:
                        titles.append(t)
            storage.upsert_event(SOURCE, build_row(usage, end, titles))
            written += 1
            if not usage.running:
                self._final.add(ext_id)
        return written
