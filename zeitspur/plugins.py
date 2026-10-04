"""Ereignis-Plugins: Quellen fuer Termine, Anrufe und Orte.

Zeitspur liefert drei Plugins mit - Outlook-Kalender, Microsoft Teams und die Standort-Historie aus
Dawarich -, aber keines ist aktiv, bevor es in den Einstellungen hinzugefuegt wurde (config.yaml:
installed_plugins). Bildschirmaufnahme, Texterkennung, Datenbank und Zeitstrahl sind der Kern und
keine Plugins.

Ein Plugin beschreibt sich selbst: Name, Beschreibung, Zugangsdaten-Felder und Einstellungen. Daraus
baut sich die Einstellungsseite, ohne dass die Oberflaeche ein Plugin beim Namen kennen muss. Die
Synchronisierung (events_sync) ruft nur fetch() der installierten Plugins auf und legt das Ergebnis
unter der Plugin-Id in calendar_events ab - die Id ist deshalb zugleich die "source" der Ereignisse
und darf sich nie aendern.

Bewusst nicht moeglich: Plugins aus fremden Dateien nachladen. Ein Plugin laeuft im selben Prozess,
der den Datenbankschluessel haelt, und koennte alles lesen. Wer eins ergaenzen will, traegt es hier
in PLUGINS ein.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, ClassVar

from . import edition

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .storage import Storage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    """Ein Eingabefeld der Plugin-Einrichtung.

    kind: "text", "secret" (verdeckt, nie im Klartext vorbelegt) oder "list" (je Zeile ein Wert).
    Zugangsdaten-Felder landen DPAPI-geschuetzt im Datenordner, Einstellungs-Felder in config.yaml -
    ihr key ist dann der Konfigurationsschluessel.
    """
    key: str
    label: str
    kind: str = "text"
    help: str = ""
    placeholder: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "help": self.help,
                "placeholder": self.placeholder}


@dataclass
class SyncContext:
    """Was ein Plugin beim Abruf braucht: Konfiguration, Datenbank (fuer eigene Caches), Zeitfenster."""
    cfg: "Config"
    storage: "Storage"
    window_start: int
    window_end: int


class EventPlugin:
    """Basisklasse. Ein Plugin liefert Ereignisse (Zeilen fuer calendar_events) fuer einen Zeitraum."""

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    label: ClassVar[str] = ""            # Kurzname an den Ereignissen ("Teams")
    color: ClassVar[str] = "#6b7280"     # Markerfarbe im Zeitstrahl
    lane: ClassVar[str] = "Termine"      # Zeile im Zeitstrahl, in der die Ereignisse erscheinen
    credential_fields: ClassVar[tuple[Field, ...]] = ()
    setting_fields: ClassVar[tuple[Field, ...]] = ()
    # Abrufen oder beobachten: Die meisten Quellen haben eine Historie, die fetch() fuer einen Zeitraum
    # abholt. Manche zeigen nur den Moment (z. B. "Teams benutzt gerade das Mikrofon") - die beobachten
    # alle paar Sekunden ueber observe() und schreiben selbst mit. Rueckwirkend gibt es dort nichts.
    observes: ClassVar[bool] = False

    def expected_errors(self) -> tuple[type[Exception], ...]:
        """Erwartbare Fehler (Netz weg, Dienst lehnt ab): Warnung ohne Stacktrace. Alles andere mit."""
        return ()

    # ---- Voraussetzungen ----------------------------------------------------------------------
    def unavailable_reason(self) -> str | None:
        """Grund, warum das Plugin auf diesem PC grundsaetzlich nicht laufen kann - sonst None."""
        return None

    def not_ready_reason(self, cfg: "Config") -> str | None:
        """Grund, warum gerade nicht synchronisiert werden kann (Zugangsdaten/Einstellungen fehlen)."""
        if self.credential_fields and not self.has_credentials():
            return "keine Zugangsdaten hinterlegt"
        return None

    # ---- Zugangsdaten (DPAPI-geschuetzt, nie in config.yaml) ----------------------------------
    def has_credentials(self) -> bool:
        return not self.credential_fields

    def save_credentials(self, values: dict[str, str]) -> None:
        raise NotImplementedError

    def reveal_credentials(self) -> dict[str, str]:
        return {}

    def clear_credentials(self) -> bool:
        return False

    # ---- Betrieb ------------------------------------------------------------------------------
    def test_connection(self, cfg: "Config") -> str:
        reason = self.unavailable_reason() or self.not_ready_reason(cfg)
        return f"Nicht bereit: {reason}" if reason else "Bereit."

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        raise NotImplementedError

    def observe(self, cfg: "Config", storage: "Storage", now_ms: int, *, titles_allowed: bool = True) -> int:
        """Nur fuer beobachtende Plugins: einmal hinsehen und mitschreiben. Liefert geschriebene Zeilen.
        titles_allowed=False, solange die Aufnahme pausiert ist - dann keine Bildschirminhalte lesen."""
        return 0

    def on_remove(self, storage: "Storage") -> None:
        """Aufraeumen beim Entfernen, zusaetzlich zu Zugangsdaten und Ereignissen (z. B. Caches)."""

    # ---- Beschreibung fuer die Oberflaeche ---------------------------------------------------
    def describe(self, cfg: "Config") -> dict:
        installed = self.id in cfg.installed_plugins
        unavailable = self.unavailable_reason()
        return {
            "id": self.id, "name": self.name, "label": self.label, "description": self.description,
            "color": self.color, "lane": self.lane,
            "installed": installed,
            "available": unavailable is None, "unavailable_reason": unavailable,
            "credential_fields": [f.as_dict() for f in self.credential_fields],
            "setting_fields": [f.as_dict() for f in self.setting_fields],
            "has_credentials": self.has_credentials(), "observes": self.observes,
            "problem": (unavailable or self.not_ready_reason(cfg)) if installed else None,
        }


# --------------------------------------------------------------------------- Outlook

class OutlookPlugin(EventPlugin):
    id = "outlook"
    name = "Outlook-Kalender"
    description = ("Termine aus dem klassischen Outlook (Desktop-Programm) als Marker im Zeitstrahl. "
                   "Keine Anmeldung nötig. Das neue Outlook bietet diese Schnittstelle nicht an.")
    lane = "Termine"
    label = "Outlook"
    color = "#3a6ea5"

    def __init__(self) -> None:
        self._calendar = None

    def _cal(self):
        if self._calendar is None:
            from .outlook import OutlookCalendar
            self._calendar = OutlookCalendar()
        return self._calendar

    def unavailable_reason(self) -> str | None:
        from .outlook import OutlookCalendar
        return None if OutlookCalendar.available() else "Das klassische Outlook ist auf diesem PC nicht verfügbar."

    def test_connection(self, cfg: "Config") -> str:
        reason = self.unavailable_reason()
        return reason or "Outlook ist erreichbar."

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        return [e.as_row() for e in self._cal().fetch(start, end)]


# --------------------------------------------------------------------------- Teams

class TeamsPlugin(EventPlugin):
    id = "teams"
    name = "Microsoft Teams"
    description = ("Ihre eigenen Teams-Anrufe (ein- und ausgehend) über Microsoft Graph. Braucht eine "
                   "App-Registrierung in Entra ID mit der Berechtigung CallRecords.Read.All (siehe README).")
    lane = "Termine"
    label = "Teams"
    color = "#5b5fc7"
    credential_fields = (
        Field("tenant_id", "Tenant-Id (Verzeichnis)", placeholder="z. B. contoso.onmicrosoft.com oder GUID"),
        Field("client_id", "Client-Id (Anwendung)", placeholder="GUID der App-Registrierung"),
        Field("client_secret", "Client-Secret", kind="secret"),
    )
    setting_fields = (
        Field("teams_user_id", "Ihre Azure-AD-Objekt-Id",
              help="Entra-Portal → Benutzer → Ihr Profil → Objekt-Id. Nötig, damit nur Ihre Anrufe "
                   "erscheinen und nicht die der ganzen Firma."),
        Field("teams_user_names", "Ihre Anzeigenamen (je Zeile, Rückfall)", kind="list",
              help="Optional, falls keine Objekt-Id: z. B. 'Mustermann, Max' und 'Max Mustermann'."),
    )

    def expected_errors(self) -> tuple[type[Exception], ...]:
        from .teams import TeamsError
        return (TeamsError,)

    def has_credentials(self) -> bool:
        from . import teams
        return teams.has_credentials()

    def save_credentials(self, values: dict[str, str]) -> None:
        from . import teams
        teams.save_credentials(values.get("tenant_id", "").strip(), values.get("client_id", "").strip(),
                               values.get("client_secret", ""))

    def reveal_credentials(self) -> dict[str, str]:
        from . import teams
        creds = teams.load_credentials() or {}
        return {k: creds.get(k, "") for k in ("tenant_id", "client_id", "client_secret")}

    def clear_credentials(self) -> bool:
        from . import teams
        return teams.clear_credentials()

    @staticmethod
    def _matcher(cfg: "Config"):
        from .teams import UserMatcher
        return UserMatcher(cfg.teams_user_id, cfg.teams_user_names)

    def not_ready_reason(self, cfg: "Config") -> str | None:
        reason = super().not_ready_reason(cfg)
        if reason:
            return reason
        if not self._matcher(cfg).active():
            # Ohne Nutzeridentitaet wuerde callRecords ALLE Anrufe des Tenants liefern -> bewusst nicht syncen.
            return ("keine Nutzer-Identität konfiguriert (Objekt-Id) - es werden keine firmenweiten "
                    "Anrufe synchronisiert")
        return None

    def test_connection(self, cfg: "Config") -> str:
        from . import teams
        creds = teams.load_credentials()
        if not creds:
            return "Es sind keine Teams-Zugangsdaten hinterlegt."
        try:
            teams.TeamsCallRecords(creds).test_connection()
            return "Verbindung zu Microsoft Graph erfolgreich."
        except Exception as e:
            return f"Fehlgeschlagen: {e}"

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        from . import teams, timeutil
        client = teams.TeamsCallRecords(teams.load_credentials())
        checked = ctx.storage.teams_seen_map(ctx.window_start, ctx.window_end)
        kept = client.fetch(start, end, matcher=self._matcher(ctx.cfg), checked=checked)
        # Klassifizierungs-Cache persistieren (ext_id -> beteiligt?), damit nicht jeder Sync neu abfragt.
        # Zeitstempel verworfener Anrufe sind unbekannt -> jetzt; die Retention greift trotzdem.
        kept_ts = {e.ext_id: e.ts_start for e in kept if e.ext_id}
        now = timeutil.now_ms()
        ctx.storage.teams_mark_seen([(ext_id, involved, kept_ts.get(ext_id, now))
                                     for ext_id, involved in checked.items()])
        return [e.as_row() for e in kept]

    def on_remove(self, storage: "Storage") -> None:
        storage.teams_clear_seen()


# --------------------------------------------------------------------------- Dawarich

class DawarichPlugin(EventPlugin):
    id = "dawarich"
    name = "Standort-Historie (Dawarich)"
    description = ("Aufenthalte und Fahrten aus Ihrer eigenen Dawarich-Instanz, mit Karte. "
                   "Der Token wird ausschließlich über HTTPS gesendet.")
    lane = "Orte"
    label = "Standort"
    color = "#0f8a6a"
    credential_fields = (
        Field("base_url", "Basis-Adresse", placeholder="https://beispiel.example.org"),
        Field("token", "Token", kind="secret"),
    )

    def expected_errors(self) -> tuple[type[Exception], ...]:
        from .dawarich import DawarichError
        return (DawarichError,)

    def has_credentials(self) -> bool:
        from . import dawarich
        return dawarich.has_credentials()

    def save_credentials(self, values: dict[str, str]) -> None:
        from . import dawarich
        dawarich.save_credentials(values.get("base_url", "").strip(), values.get("token", ""))

    def reveal_credentials(self) -> dict[str, str]:
        from . import dawarich
        creds = dawarich.load_credentials() or {}
        return {"base_url": creds.get("base_url", ""), "token": creds.get("token", "")}

    def clear_credentials(self) -> bool:
        from . import dawarich
        return dawarich.clear_credentials()

    def test_connection(self, cfg: "Config") -> str:
        from . import dawarich
        creds = dawarich.load_credentials()
        if not creds:
            return "Es sind keine Dawarich-Zugangsdaten hinterlegt."
        try:
            return dawarich.DawarichClient(creds).test_connection()
        except Exception as e:
            return f"Fehlgeschlagen: {e}"

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        from . import dawarich
        return [e.as_row() for e in dawarich.DawarichClient(dawarich.load_credentials()).fetch(start, end)]


# --------------------------------------------------------------------------- Teams lokal

class TeamsLocalPlugin(EventPlugin):
    id = "teams_local"
    name = "Teams-Gespräche (lokal)"
    description = ("Erkennt Teams-Anrufe und -Besprechungen daran, dass Teams auf diesem PC das Mikrofon "
                   "benutzt – ohne Entra-App, ohne Zugangsdaten, ohne Netz. Nur Gespräche an diesem PC: Was am "
                   "Handy oder Tischtelefon läuft, erscheint nicht; das kennt nur das Plugin „Microsoft Teams“. "
                   "Zeiten genau, Gesprächspartner nur, wenn Teams sie im Fenstertitel zeigt. Erfasst ab dem "
                   "Hinzufügen.")
    lane = "Termine"
    label = "Teams (lokal)"
    color = "#8b8fe8"
    observes = True

    def __init__(self) -> None:
        self._recorder = None

    def _rec(self):
        if self._recorder is None:
            from .teams_local import CallRecorder
            self._recorder = CallRecorder()
        return self._recorder

    def unavailable_reason(self) -> str | None:
        from .teams_local import mic_access_reason
        return mic_access_reason()

    def test_connection(self, cfg: "Config") -> str:
        from datetime import datetime

        from .teams_local import mic_access_reason, read_mic_usage
        reason = mic_access_reason()
        if reason:
            return reason
        usages = read_mic_usage()
        if not usages:
            return "Bereit. Teams hat das Mikrofon auf diesem PC noch nie benutzt – das erste Gespräch wird erfasst."
        last = max(usages, key=lambda u: u.start_ms)
        if last.running:
            return "Bereit. Teams benutzt gerade das Mikrofon – das laufende Gespräch wird erfasst."
        fmt = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%d.%m. %H:%M:%S")  # noqa: E731
        return f"Bereit. Letzte Mikrofon-Nutzung durch Teams: {fmt(last.start_ms)} bis {fmt(last.stop_ms)}."

    def observe(self, cfg: "Config", storage: "Storage", now_ms: int, *, titles_allowed: bool = True) -> int:
        return self._rec().observe(storage, now_ms, titles_allowed=titles_allowed)

    def on_remove(self, storage: "Storage") -> None:
        self._recorder = None   # vergisst, was schon abgeschlossen war - neu hinzugefuegt beginnt es frisch


# --------------------------------------------------------------------------- Registry

def _registry(locations: bool) -> tuple[EventPlugin, ...]:
    """Die Plugins dieser Ausgabe. Ohne Standort-Funktionen (Release-Build) fehlt Dawarich ganz."""
    plugins: list[EventPlugin] = [OutlookPlugin(), TeamsPlugin(), TeamsLocalPlugin()]
    if locations:
        plugins.append(DawarichPlugin())
    return tuple(plugins)


PLUGINS: tuple[EventPlugin, ...] = _registry(edition.LOCATIONS)
_unknown_logged: set[str] = set()


class UnknownPlugin(KeyError):
    pass


def get(plugin_id: str) -> EventPlugin:
    for p in PLUGINS:
        if p.id == plugin_id:
            return p
    raise UnknownPlugin(f"Unbekanntes Plugin: {plugin_id}")


def installed(cfg: "Config") -> list[EventPlugin]:
    """Die installierten Plugins in fester Reihenfolge. Unbekannte Namen werden ignoriert."""
    wanted = set(cfg.installed_plugins)
    # Nur einmal je Name melden: installed() laeuft im Beobachter alle paar Sekunden. Typischer Fall ist
    # ein Release-Build ueber einer Installation, in der ein dort fehlendes Plugin hinzugefuegt war.
    unknown = wanted - {p.id for p in PLUGINS} - _unknown_logged
    if unknown:
        _unknown_logged.update(unknown)
        log.warning("Plugins in dieser Ausgabe nicht enthalten, ignoriert: %s", ", ".join(sorted(unknown)))
    return [p for p in PLUGINS if p.id in wanted]


def display(source: str) -> dict[str, str]:
    """Anzeige-Angaben fuer Ereignisse einer Quelle (Kurzname, Farbe, Zeitstrahl-Zeile)."""
    for p in PLUGINS:
        if p.id == source:
            return {"label": p.label, "color": p.color, "lane": p.lane}
    return {"label": source, "color": EventPlugin.color, "lane": EventPlugin.lane}
