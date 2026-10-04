"""Gespraeche und Meetings in allen Apps - erkannt daran, dass eine App das Mikrofon benutzt.

Dieselbe Quelle wie bei "Teams-Gespraeche (lokal)" (siehe teams_local): Windows haelt je App fest, wann sie
Mikrofon und Kamera zuletzt benutzt hat - dieselbe Quelle, aus der das Mikrofon-Symbol in der Taskleiste
gespeist wird:

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone
        \\<Paketname>                    (Apps aus dem Store, z. B. WhatsApp)
        \\NonPackaged\\C:#...#Zoom.exe    (klassische Programme, Pfad mit '#')
    (dasselbe unter ...\\webcam)

Weil nur die LETZTE Nutzung je App gespeichert wird, schaut Zeitspur alle paar Sekunden nach und schreibt jedes
Gespraech selbst mit. Rueckwirkend gibt es nichts. Wer gesprochen hat, verraet das Mikrofon nicht. Nur bei
Meetings im Browser wird der Titel des Meeting-Tabs uebernommen - und nur, wenn er eindeutig nach einem Meeting
aussieht (Google Meet, Zoom, Teams, Webex, Jitsi ...); andere Tabs bleiben aussen vor.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime

from .teams_local import MicUsage, _usage_from_values

log = logging.getLogger(__name__)

SOURCE = "calls_local"
CONSENT_ROOT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
MIN_SESSION_MS = 5_000       # kuerzere Mikrofon-Nutzung (Geraetetest, Fehlklick) ist kein Gespraech
MAX_TITLES = 5


@dataclass(frozen=True)
class App:
    name: str
    kind: str    # call | browser | remote | recording | voice | system | other


_CALL = "call"
# Programmdateien (klein geschrieben) -> App
_EXE: dict[str, App] = {
    **{exe: App("Microsoft Teams", _CALL) for exe in ("ms-teams.exe", "teams.exe")},
    "zoom.exe": App("Zoom", _CALL),
    "slack.exe": App("Slack", _CALL),
    **{exe: App("Webex", _CALL) for exe in ("ciscocollabhost.exe", "webexmta.exe", "atmgr.exe", "webex.exe",
                                            "ciscowebexstart.exe")},
    "skype.exe": App("Skype", _CALL),
    "discord.exe": App("Discord", _CALL),
    "signal.exe": App("Signal", _CALL),
    "whatsapp.exe": App("WhatsApp", _CALL),
    "telegram.exe": App("Telegram", _CALL),
    **{exe: App("STARFACE", _CALL) for exe in ("starface app.exe", "starface.exe")},
    "3cxdesktopapp.exe": App("3CX", _CALL),
    "microsip.exe": App("MicroSIP", _CALL),
    **{exe: App("Zoiper", _CALL) for exe in ("zoiper.exe", "zoiper5.exe")},
    **{exe: App("PhonerLite", _CALL) for exe in ("phonerlite.exe", "phoner.exe")},
    "linphone.exe": App("Linphone", _CALL),
    **{exe: App("GoTo Meeting", _CALL) for exe in ("g2mcomm.exe", "goto.exe", "gotomeeting.exe")},
    "ringcentral.exe": App("RingCentral", _CALL),
    "jitsi meet.exe": App("Jitsi Meet", _CALL),
    "element.exe": App("Element", _CALL),
    "msedge.exe": App("Edge", "browser"),
    "chrome.exe": App("Chrome", "browser"),
    "firefox.exe": App("Firefox", "browser"),
    "brave.exe": App("Brave", "browser"),
    "opera.exe": App("Opera", "browser"),
    "vivaldi.exe": App("Vivaldi", "browser"),
    "teamviewer.exe": App("TeamViewer", "remote"),
    "anydesk.exe": App("AnyDesk", "remote"),
    "plaud.exe": App("Plaud", "recording"),
    "audacity.exe": App("Audacity", "recording"),
    "obs64.exe": App("OBS Studio", "recording"),
    "chatgpt.exe": App("ChatGPT", "voice"),
    **{exe: App("Windows", "system") for exe in ("shellhost.exe", "svchost.exe", "audiodg.exe",
                                                 "systemsettings.exe")},
    **{exe: App("FortiClient", "system") for exe in ("forticlient.exe", "fortitray.exe")},
}
# Store-Apps: Paketname vor dem '_' (klein geschrieben) -> App
_PACKAGES: dict[str, App] = {
    "msteams": App("Microsoft Teams", _CALL),
    "microsoft.skypeapp": App("Skype", _CALL),
    "5319275a.whatsappdesktop": App("WhatsApp", _CALL),
    "91750d7e.slack": App("Slack", _CALL),
    "claude": App("Claude", "voice"),
    "openai.chatgpt-desktop": App("ChatGPT", "voice"),
    "microsoft.copilot": App("Copilot", "voice"),
    "microsoft.windowssoundrecorder": App("Audiorekorder", "recording"),
    "microsoft.windowscamera": App("Kamera", "system"),
    "windows.immersivecontrolpanel": App("Windows-Einstellungen", "system"),
    "microsoft.bioenrollment": App("Windows Hello", "system"),
    "microsoft.windows.cortana": App("Cortana", "system"),
}
# Art -> (Kategorie in calendar_events, Betreff)
_KINDS = {
    "call": ("conversation", "Gespräch: {app}"),
    "browser": ("conversation", "Gespräch im Browser ({app})"),
    "remote": ("remote", "Fernwartung: {app}"),
    "recording": ("recording", "Aufnahme: {app}"),
    "voice": ("voice", "Spracheingabe: {app}"),
    "other": ("microphone", "Mikrofon: {app}"),
}
# Ein Browser-Tab zaehlt nur als Meeting, wenn der Titel eindeutig danach aussieht
MEETING_TITLE = re.compile(r"\b(Meet|Zoom|Teams|Webex|Jitsi|Whereby|BigBlueButton|GoTo|Skype|Discord|Slack|"
                           r"Huddle|Besprechung|Meeting)\b", re.IGNORECASE)
_BROWSER_SUFFIX = re.compile(r"\s+(?:und|and)\s+\d+\s+(?:weitere|more)\s+(?:Seiten?|pages?).*$|"
                             r"\s+[-–—]\s+(?:[^-–—]*[-–—]\s+)?(?:Microsoft​?\s*Edge|Google Chrome|Mozilla Firefox|"
                             r"Brave|Opera|Vivaldi)\s*$", re.IGNORECASE)


def exe_of(key: str) -> str | None:
    """Programmdatei eines klassischen Programms (Schluessel ist der Pfad mit '#'), sonst None."""
    if "#" in key or key.lower().endswith(".exe"):
        return key.rsplit("#", 1)[-1]
    return None


def identify(key: str) -> App:
    exe = exe_of(key)
    if exe is not None:
        return _EXE.get(exe.lower()) or App(re.sub(r"\.exe$", "", exe, flags=re.IGNORECASE), "other")
    family = key.split("_", 1)[0]
    return _PACKAGES.get(family.lower()) or App(family.rsplit(".", 1)[-1] or family, "other")


def clean_browser_title(title: str) -> str:
    """'Meet – abc-defg-hij und 3 weitere Seiten - Persoenlich – Microsoft Edge' -> 'Meet – abc-defg-hij'"""
    previous = None
    while previous != title:
        previous = title
        title = _BROWSER_SUFFIX.sub("", title).strip()
    return title


def access_reason() -> str | None:
    """Grund, warum sich die Mikrofon-Nutzung nicht auslesen laesst - sonst None."""
    if sys.platform != "win32":
        return "Nur unter Windows verfügbar."
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONSENT_ROOT + r"\microphone") as key:
            value = winreg.QueryValueEx(key, "Value")[0]
    except OSError:
        return "Windows führt auf diesem PC kein Mikrofon-Protokoll."
    if str(value).lower() == "deny":
        return "Der Mikrofonzugriff für Apps ist in den Windows-Datenschutzeinstellungen gesperrt."
    return None


def read_usages(capability: str) -> dict[str, MicUsage]:
    """Letzte Nutzung je App-Schluessel fuer 'microphone' oder 'webcam'."""
    if sys.platform != "win32":
        return {}
    import winreg

    base = rf"{CONSENT_ROOT}\{capability}"
    out: dict[str, MicUsage] = {}
    for sub in ("", r"\NonPackaged"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + sub) as key:
                names = [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
        except OSError:
            continue
        for name in names:
            if name == "NonPackaged":
                continue
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{base}{sub}\{name}") as key:
                    start = winreg.QueryValueEx(key, "LastUsedTimeStart")[0]
                    stop = winreg.QueryValueEx(key, "LastUsedTimeStop")[0]
            except OSError:
                continue
            if usage := _usage_from_values(name, start, stop):
                out[name] = usage
    return out


def window_titles(exe_name: str) -> list[str]:
    """Titel aller sichtbaren Fenster eines Programms (auch der nicht vorne liegenden)."""
    if sys.platform != "win32":
        return []
    import psutil
    import win32gui
    import win32process

    wanted = exe_name.lower()
    titles: list[str] = []
    names: dict[int, str] = {}

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        if pid not in names:
            try:
                names[pid] = psutil.Process(pid).name().lower()
            except Exception:
                names[pid] = ""
        if names[pid] == wanted and title not in titles:
            titles.append(title)
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        log.debug("Fenster von %s liessen sich nicht aufzaehlen", exe_name, exc_info=True)
    return titles


def _short(key: str) -> str:
    return hashlib.sha1(key.lower().encode("utf-8")).hexdigest()[:8]


def build_row(key: str, app: App, usage: MicUsage, end_ms: int, *, video: bool, titles: list[str]) -> dict:
    category, template = _KINDS.get(app.kind, _KINDS["other"])
    subject = template.format(app=app.name) + (" mit Video" if video else "")
    if app.kind == "browser" and titles:
        subject += f": {titles[0]}"
    extra = {"app": app.name, "app_kind": app.kind, "video": video, "in_progress": usage.running,
             "detected_by": "microphone"}
    if titles:
        extra["window_titles"] = titles[:MAX_TITLES]
    return {
        "ext_id": f"mic-{_short(key)}-{usage.start_ms}",
        "ts_start": usage.start_ms, "ts_end": max(end_ms, usage.start_ms),
        "subject": subject, "location": app.name, "organizer": None, "attendees": None,
        "category": category, "extra": json.dumps(extra, ensure_ascii=False),
    }


def _overlaps(other: MicUsage | None, start_ms: int, end_ms: int, now_ms: int) -> bool:
    if other is None:
        return False
    other_end = now_ms if other.running else other.stop_ms
    return other.start_ms < end_ms and other_end > start_ms


class CallsRecorder:
    """Schreibt jede Mikrofon-Nutzung als Gespraech mit (Muster wie teams_local.CallRecorder).

    Zustandslos ueber Neustarts hinweg: Was schon gespeichert ist, wird aus der Datenbank gelesen und ergaenzt.
    Ein laufendes Gespraech wird bei jedem Blick verlaengert (in_progress), ein beendetes einmal mit der genauen
    Endzeit abgeschlossen und danach nicht mehr angefasst.
    """

    def __init__(self, read=read_usages, read_titles=window_titles):
        self._read = read
        self._titles = read_titles
        self._final: set[str] = set()

    def observe(self, storage, now_ms: int, *, titles_allowed: bool = True, ignore: list[str] = (),
                include_other: bool = True, skip_teams: bool = False) -> int:
        ignored = {i.strip().lower() for i in ignore if i.strip()}
        mic = self._read("microphone")
        cam = self._read("webcam") if mic else {}
        written = 0
        for key, usage in mic.items():
            app = identify(key)
            exe = (exe_of(key) or "").lower()
            if app.kind == "system" or app.name.lower() in ignored or (exe and exe in ignored):
                continue
            if not include_other and app.kind not in ("call", "browser"):
                continue
            if skip_teams and app.name == "Microsoft Teams":
                continue   # erfasst "Teams-Gespraeche (lokal)" - samt Gegenueber
            row_id = f"mic-{_short(key)}-{usage.start_ms}"
            if row_id in self._final:
                continue
            end = now_ms if usage.running else usage.stop_ms
            if end - usage.start_ms < MIN_SESSION_MS:
                continue
            stored = storage.event_by_ext_id(SOURCE, row_id, usage.start_ms)
            old = json.loads(stored["extra"] or "{}") if stored else {}
            if stored and not usage.running and not old.get("in_progress"):
                self._final.add(row_id)          # schon abgeschlossen gespeichert (z. B. vor einem Neustart)
                continue
            video = bool(old.get("video")) or _overlaps(cam.get(key), usage.start_ms, end, now_ms)
            titles = list(old.get("window_titles") or [])
            if usage.running and titles_allowed and app.kind == "browser" and exe:
                for t in self._titles(exe):
                    if MEETING_TITLE.search(t):
                        cleaned = clean_browser_title(t)
                        if cleaned and cleaned not in titles:
                            titles.append(cleaned)
            storage.upsert_event(SOURCE, build_row(key, app, usage, end, video=video, titles=titles))
            written += 1
            if not usage.running:
                self._final.add(row_id)
        return written


def describe_recent(read=read_usages) -> str:
    """Fuer "Verbindung testen": welche Apps zuletzt das Mikrofon benutzt haben."""
    usages = sorted(read("microphone").items(), key=lambda kv: kv[1].start_ms, reverse=True)
    apps = [(identify(k), u) for k, u in usages]
    apps = [(a, u) for a, u in apps if a.kind != "system"]
    if not apps:
        return "Bereit. Bisher hat keine App das Mikrofon benutzt – das erste Gespräch wird erfasst."
    fmt = lambda u: datetime.fromtimestamp(u.start_ms / 1000).strftime("%d.%m. %H:%M")  # noqa: E731
    teile = [f"{a.name} ({'gerade' if u.running else fmt(u)})" for a, u in apps[:5]]
    return "Bereit. Zuletzt am Mikrofon: " + ", ".join(teile) + "."
