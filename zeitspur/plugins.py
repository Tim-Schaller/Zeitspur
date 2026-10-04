"""Plugins: Quellen fuer Termine, Gespraeche, Mails, Mitteilungen, Web, Entwicklung, PC-Zeiten und Orte.

Zeitspur liefert alle Plugins mit, aber keines ist aktiv, bevor es im Plugin-Browser (oder bei der
Ersteinrichtung) hinzugefuegt wurde (config.yaml: installed_plugins). Bildschirmaufnahme, Texterkennung,
Datenbank und Zeitstrahl sind der Kern und keine Plugins.

Ein Plugin beschreibt sich selbst: Name, Kategorie, Kurzbeschreibung, was es speichert, Datenschutz-Hinweis,
Einrichtungsschritte, Zugangsdaten- und Einstellungsfelder. Daraus baut sich der Plugin-Browser, ohne dass die
Oberflaeche ein Plugin beim Namen kennen muss. Die Synchronisierung (events_sync) ruft nur fetch() bzw. observe()
der installierten Plugins auf und legt das Ergebnis unter der Plugin-Id in calendar_events ab - die Id ist
deshalb zugleich die "source" der Ereignisse und darf sich nie aendern.

Bewusst nicht moeglich: Plugins aus fremden Dateien nachladen. Ein Plugin laeuft im selben Prozess, der den
Datenbankschluessel haelt, und koennte alles lesen. Wer eins ergaenzen will, traegt es hier in PLUGINS ein.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar

from . import edition

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .storage import Storage

log = logging.getLogger(__name__)

# Gruppen im Plugin-Browser, in dieser Reihenfolge
CATEGORIES = ("Kalender & Mail", "Gespräche & Meetings", "Mitteilungen", "Web", "Entwicklung", "PC & Netzwerk",
              "Orte")
# Zeilen im Zeitstrahl, in dieser Reihenfolge (eine Zeile erscheint nur, wenn sie an dem Tag Ereignisse hat)
LANES = ("Termine", "Gespräche", "Mails", "Mitteilungen", "Web", "Entwicklung", "PC", "Orte")
# Anzeige der Ereignis-Kategorien (Spalte category in calendar_events) - fuer Zeitstrahl und Claude
CATEGORY_LABELS = {
    "meeting": "Besprechung", "appointment": "Termin", "call": "Anruf",
    "conversation": "Gespräch", "remote": "Fernwartung", "recording": "Aufnahme", "voice": "Spracheingabe",
    "microphone": "Mikrofon", "notification": "Mitteilung", "web": "Surfen",
    "mail_sent": "Mail gesendet", "mail_received": "Mail empfangen",
    "commit": "Commit", "github": "GitHub", "pc_on": "PC an", "wifi": "WLAN",
    "visit": "Aufenthalt", "track": "Fahrt",
}


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "ja", "yes", "on", "an", "ein")


@dataclass(frozen=True)
class Field:
    """Ein Eingabefeld der Plugin-Einrichtung.

    kind: "text", "secret" (verdeckt, nie im Klartext vorbelegt), "secretlist" (mehrzeilig und geheim, etwa
    Kalender-Links), "list" (je Zeile ein Wert), "folders" (Ordnerliste mit Auswahlknopf), "bool", "number",
    "select" (options: (Wert, Anzeige)). Zugangsdaten-Felder landen DPAPI-geschuetzt im Datenordner,
    Einstellungs-Felder in config.yaml.
    """
    key: str
    label: str
    kind: str = "text"
    help: str = ""
    placeholder: str = ""
    default: Any = None
    options: tuple[tuple[str, str], ...] = ()
    optional: bool = False

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "help": self.help,
                "placeholder": self.placeholder, "default": copy.deepcopy(self.default),
                "options": [list(o) for o in self.options], "optional": self.optional}

    def coerce(self, raw: Any) -> Any:
        """Formularwert -> gespeicherter Wert. Wirft ValueError mit verstaendlicher Meldung."""
        if self.kind in ("list", "folders", "secretlist"):
            if raw is None:
                return []
            items = raw.splitlines() if isinstance(raw, str) else [str(x) for x in raw]
            return [i.strip() for i in items if i.strip()]
        if self.kind == "bool":
            return as_bool(raw) if raw is not None else bool(self.default)
        if self.kind == "number":
            if raw is None or str(raw).strip() == "":
                return self.default
            value = float(str(raw).replace(",", "."))
            return int(value) if value.is_integer() else value
        if self.kind == "select":
            value = "" if raw is None else str(raw)
            if value not in {o[0] for o in self.options}:
                raise ValueError("ungültige Auswahl")
            return value
        if raw is None:
            return ""
        return str(raw) if self.kind == "secret" else str(raw).strip()


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


@dataclass
class SyncContext:
    """Was ein Plugin beim Abruf braucht: Konfiguration, Datenbank (fuer eigene Caches), Zeitfenster."""
    cfg: "Config"
    storage: "Storage"
    window_start: int
    window_end: int


class PluginError(Exception):
    """Erwartbarer Fehler eines Plugins (Dienst nicht erreichbar, Zugang abgelehnt): Warnung ohne Stacktrace."""


class EventPlugin:
    """Basisklasse. Ein Plugin liefert Ereignisse (Zeilen fuer calendar_events) fuer einen Zeitraum."""

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    summary: ClassVar[str] = ""          # eine Zeile fuer die Kachel im Plugin-Browser
    description: ClassVar[str] = ""      # ausfuehrlich, fuer die Detailansicht
    category: ClassVar[str] = "Kalender & Mail"
    icon: ClassVar[str] = "•"
    label: ClassVar[str] = ""            # Kurzname an den Ereignissen ("Teams")
    color: ClassVar[str] = "#6b7280"     # Markerfarbe im Zeitstrahl
    lane: ClassVar[str] = "Termine"      # Zeile im Zeitstrahl, in der die Ereignisse erscheinen
    network: ClassVar[str] = "local"     # "local": nur dieser PC | "online": fragt einen Dienst im Netz
    account: ClassVar[str] = "none"      # "none" | "token" (Zugangsdaten/Link) | "app" (App-Registrierung, Admin)
    records: ClassVar[str] = ""          # was genau gespeichert wird
    privacy: ClassVar[str] = ""          # Hinweis bei heiklen Daten (im Browser hervorgehoben)
    setup_steps: ClassVar[tuple[str, ...]] = ()
    credential_fields: ClassVar[tuple[Field, ...]] = ()
    setting_fields: ClassVar[tuple[Field, ...]] = ()
    # Einstellungen als eigene Schluessel in config.yaml statt unter plugin_settings (Teams, aus der Zeit davor)
    legacy_settings: ClassVar[bool] = False
    # Abrufen oder beobachten: Die meisten Quellen haben eine Historie, die fetch() fuer einen Zeitraum
    # abholt. Manche zeigen nur den Moment (z. B. "Teams benutzt gerade das Mikrofon") - die beobachten
    # alle paar Sekunden ueber observe() und schreiben selbst mit. Rueckwirkend gibt es dort nichts.
    observes: ClassVar[bool] = False

    def expected_errors(self) -> tuple[type[Exception], ...]:
        """Erwartbare Fehler (Netz weg, Dienst lehnt ab): Warnung ohne Stacktrace. Alles andere mit."""
        return (PluginError,)

    # ---- Voraussetzungen ----------------------------------------------------------------------
    def unavailable_reason(self) -> str | None:
        """Grund, warum das Plugin auf diesem PC grundsaetzlich nicht laufen kann - sonst None."""
        return None

    def not_ready_reason(self, cfg: "Config") -> str | None:
        """Grund, warum gerade nicht synchronisiert werden kann (Zugangsdaten/Einstellungen fehlen)."""
        if self.requires_credentials and not self.has_credentials():
            return "keine Zugangsdaten hinterlegt"
        settings = self.settings(cfg)
        missing = [f.label for f in self.setting_fields if not f.optional and _empty(settings.get(f.key))]
        if missing:
            return "Einstellung fehlt: " + ", ".join(missing)
        return None

    # ---- Einstellungen (config.yaml) ------------------------------------------------------------
    def settings(self, cfg: "Config") -> dict[str, Any]:
        """Aktuelle Einstellungen, fehlende mit Vorgabe."""
        if self.legacy_settings:
            return {f.key: copy.deepcopy(getattr(cfg, f.key)) for f in self.setting_fields}
        stored = (cfg.plugin_settings or {}).get(self.id) or {}
        return {f.key: copy.deepcopy(stored.get(f.key, f.default)) for f in self.setting_fields}

    def clean_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        """Formularwerte -> gepruefte Einstellungen. Wirft ValueError."""
        out: dict[str, Any] = {}
        for f in self.setting_fields:
            try:
                out[f.key] = f.coerce(values.get(f.key))
            except (TypeError, ValueError) as e:
                raise ValueError(f"{f.label}: {e}") from e
        self.check_settings(out)
        return out

    def check_settings(self, values: dict[str, Any]) -> None:
        """Plugin-spezifische Pruefung der Einstellungen - wirft ValueError."""

    # ---- Zugangsdaten (DPAPI-geschuetzt, nie in config.yaml) ----------------------------------
    @property
    def requires_credentials(self) -> bool:
        return any(not f.optional for f in self.credential_fields)

    def has_credentials(self) -> bool:
        if not self.credential_fields:
            return True
        from . import credentials
        return credentials.exists(self.id)

    def clean_credentials(self, values: dict[str, Any]) -> dict[str, str]:
        """Formularwerte -> zu speichernde Zugangsdaten. Wirft ValueError."""
        clean: dict[str, str] = {}
        for f in self.credential_fields:
            raw = values.get(f.key)
            if f.kind == "secretlist":
                value = "\n".join(f.coerce(raw))
            else:
                value = "" if raw is None else str(raw)
                if f.kind != "secret":
                    value = value.strip()
            if not value.strip() and not f.optional:
                raise ValueError(f"{f.label} ist erforderlich.")
            clean[f.key] = value
        self.check_credentials(clean)
        return clean

    def check_credentials(self, values: dict[str, str]) -> None:
        """Plugin-spezifische Pruefung der Zugangsdaten - wirft ValueError (ohne den geheimen Wert zu nennen)."""

    def save_credentials(self, values: dict[str, str]) -> None:
        from . import credentials
        credentials.save(self.id, values)

    def load_credentials(self) -> dict[str, str]:
        from . import credentials
        return credentials.load(self.id) or {}

    def reveal_credentials(self) -> dict[str, str]:
        return self.load_credentials()

    def clear_credentials(self) -> bool:
        if not self.credential_fields:
            return False
        from . import credentials
        return credentials.clear(self.id)

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
            "id": self.id, "name": self.name, "label": self.label, "summary": self.summary,
            "description": self.description, "category": self.category, "icon": self.icon,
            "color": self.color, "lane": self.lane, "network": self.network, "account": self.account,
            "records": self.records, "privacy": self.privacy, "setup_steps": list(self.setup_steps),
            "installed": installed,
            "available": unavailable is None, "unavailable_reason": unavailable,
            "credential_fields": [f.as_dict() for f in self.credential_fields],
            "setting_fields": [f.as_dict() for f in self.setting_fields],
            "settings": self.settings(cfg),
            "has_credentials": self.has_credentials(), "requires_credentials": self.requires_credentials,
            "observes": self.observes,
            "problem": (unavailable or self.not_ready_reason(cfg)) if installed else None,
        }


# =========================================================================== Kalender & Mail

class OutlookPlugin(EventPlugin):
    id = "outlook"
    name = "Outlook-Kalender"
    summary = "Termine aus dem klassischen Outlook – ohne Anmeldung."
    description = ("Termine aus dem klassischen Outlook (Desktop-Programm) als Marker im Zeitstrahl. "
                   "Keine Anmeldung nötig. Das neue Outlook bietet diese Schnittstelle nicht an – dafür gibt es "
                   "das Plugin „Kalender per ICS-Link“.")
    category = "Kalender & Mail"
    icon = "📅"
    lane = "Termine"
    label = "Outlook"
    color = "#3a6ea5"
    records = "Betreff, Beginn und Ende, Ort, Organisator und Teilnehmer Ihrer Termine (±14 Tage)."
    setup_steps = ("Das klassische Outlook muss installiert und eingerichtet sein.",
                   "Plugin hinzufügen – fertig. Die Termine erscheinen nach dem nächsten Abgleich.")

    def __init__(self) -> None:
        self._calendar = None

    def _cal(self):
        if self._calendar is None:
            from .outlook import OutlookCalendar
            self._calendar = OutlookCalendar()
        return self._calendar

    def expected_errors(self) -> tuple[type[Exception], ...]:
        from .outlook import OutlookError
        return (PluginError, OutlookError)

    def unavailable_reason(self) -> str | None:
        from .outlook import OutlookCalendar
        return None if OutlookCalendar.available() else "Das klassische Outlook ist auf diesem PC nicht verfügbar."

    def test_connection(self, cfg: "Config") -> str:
        reason = self.unavailable_reason()
        return reason or "Outlook ist erreichbar."

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        return [e.as_row() for e in self._cal().fetch(start, end)]


class IcsCalendarPlugin(EventPlugin):
    id = "ics"
    name = "Kalender per ICS-Link"
    summary = "Google, iCloud, neues Outlook, Nextcloud … über den geheimen Kalender-Link."
    description = ("Liest Kalender über ihre iCal-Adresse (ICS-Link) – das bieten Google Kalender, iCloud, "
                   "Outlook.com und das neue Outlook (Kalender veröffentlichen), Nextcloud und viele andere. "
                   "Serientermine werden aufgelöst. Kein Login: Der Link selbst ist der Schlüssel und wird wie ein "
                   "Passwort behandelt.")
    category = "Kalender & Mail"
    icon = "🗓️"
    lane = "Termine"
    label = "Kalender"
    color = "#0b7285"
    network = "online"
    account = "token"
    records = "Betreff, Beginn und Ende, Ort, Organisator und Teilnehmer der Termine (±14 Tage)."
    privacy = ("Der geheime Link gibt Lesezugriff auf den ganzen Kalender – er wird DPAPI-geschützt gespeichert "
               "und erscheint nie in Protokollen oder Fehlermeldungen.")
    setup_steps = (
        "Google Kalender: Einstellungen → Kalender auswählen → „Privatadresse im iCal-Format“ kopieren.",
        "Outlook (neu) / Outlook.com: Einstellungen → Kalender → Freigegebene Kalender → „Kalender veröffentlichen“, "
        "Detailstufe „Alle Details“ → ICS-Link kopieren.",
        "iCloud: Kalender-App → Kalender teilen → „Öffentlicher Kalender“ → Link kopieren (webcal:// geht auch).",
        "Hier je Zeile einen Link eintragen, optional mit Namen davor: Arbeit | https://…",
    )
    credential_fields = (
        Field("urls", "Kalender-Links (je Zeile einer)", kind="secretlist",
              placeholder="Arbeit | https://… oder webcal://…",
              help="Optional mit Namen und senkrechtem Strich davor. Nur https/webcal."),
    )

    def check_credentials(self, values: dict[str, str]) -> None:
        from .ics_calendar import parse_lines
        parse_lines(values.get("urls", ""))   # wirft ValueError mit Zeilennummer, ohne den Link zu nennen

    def test_connection(self, cfg: "Config") -> str:
        from .ics_calendar import test_calendars
        if not self.has_credentials():
            return "Es sind keine Kalender-Links hinterlegt."
        try:
            return test_calendars(self.load_credentials().get("urls", ""))
        except PluginError as e:
            return f"Fehlgeschlagen: {e}"

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        from .ics_calendar import fetch_all
        return fetch_all(self.load_credentials().get("urls", ""), start, end)


class OutlookMailPlugin(EventPlugin):
    id = "outlook_mail"
    name = "Outlook-Mails"
    summary = "Gesendete und empfangene Mails: Betreff, Absender, Zeit – kein Inhalt."
    description = ("Zeigt, wann Sie welche Mails geschrieben und bekommen haben – aus dem klassischen Outlook, "
                   "ohne Anmeldung. Gespeichert werden Betreff, Absender bzw. Empfänger und Uhrzeit, nie der "
                   "Mailtext oder Anhänge.")
    category = "Kalender & Mail"
    icon = "✉️"
    lane = "Mails"
    label = "Mail"
    color = "#5c7cfa"
    records = "Betreff, Absender bzw. Empfänger und Zeitpunkt von Mails aus „Gesendete Elemente“ und dem Posteingang."
    privacy = "Betreffzeilen und Namen Ihrer Korrespondenz landen (verschlüsselt) in der Datenbank – keine Mailtexte."
    setup_steps = ("Das klassische Outlook muss installiert und eingerichtet sein.",
                   "Plugin hinzufügen; unten wählen, ob gesendete, empfangene oder beide Mails erscheinen.")
    setting_fields = (
        Field("sent", "Gesendete Mails", kind="bool", default=True, optional=True),
        Field("received", "Empfangene Mails (Posteingang)", kind="bool", default=True, optional=True),
    )

    def expected_errors(self) -> tuple[type[Exception], ...]:
        from .outlook import OutlookError
        return (PluginError, OutlookError)

    def unavailable_reason(self) -> str | None:
        from .outlook import OutlookCalendar
        return None if OutlookCalendar.available() else "Das klassische Outlook ist auf diesem PC nicht verfügbar."

    def not_ready_reason(self, cfg: "Config") -> str | None:
        s = self.settings(cfg)
        if not s.get("sent") and not s.get("received"):
            return "weder gesendete noch empfangene Mails ausgewählt"
        return None

    def test_connection(self, cfg: "Config") -> str:
        return self.unavailable_reason() or "Outlook ist erreichbar."

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        from .outlook_mail import OutlookMail
        s = self.settings(ctx.cfg)
        return OutlookMail().fetch(start, end, sent=bool(s.get("sent")), received=bool(s.get("received")))


# =========================================================================== Gespraeche & Meetings

class TeamsPlugin(EventPlugin):
    id = "teams"
    name = "Microsoft Teams"
    summary = "Ihre Teams-Anrufe mit Gesprächspartner – über Microsoft Graph (Admin-Freigabe nötig)."
    description = ("Ihre eigenen Teams-Anrufe (ein- und ausgehend) über Microsoft Graph, mit Gesprächspartner "
                   "und Dauer. Braucht eine App-Registrierung in Entra ID mit der Berechtigung "
                   "CallRecords.Read.All und Admin-Zustimmung (siehe README). Ohne Admin: „Teams-Gespräche (lokal)“.")
    category = "Gespräche & Meetings"
    icon = "📞"
    lane = "Gespräche"
    label = "Teams"
    color = "#5b5fc7"
    network = "online"
    account = "app"
    records = "Beginn, Ende, Richtung und Gesprächspartner Ihrer Teams-Anrufe."
    setup_steps = ("Entra-Portal: App registrieren, Anwendungsberechtigung CallRecords.Read.All hinzufügen und "
                   "Administrator-Zustimmung erteilen.",
                   "Unter „Zertifikate & Geheimnisse“ ein Client-Secret anlegen.",
                   "Tenant-Id, Client-Id und Secret hier eintragen, dazu Ihre Objekt-Id (Entra → Benutzer → Profil).")
    credential_fields = (
        Field("tenant_id", "Tenant-Id (Verzeichnis)", placeholder="z. B. contoso.onmicrosoft.com oder GUID"),
        Field("client_id", "Client-Id (Anwendung)", placeholder="GUID der App-Registrierung"),
        Field("client_secret", "Client-Secret", kind="secret"),
    )
    setting_fields = (
        Field("teams_user_id", "Ihre Azure-AD-Objekt-Id", optional=True,
              help="Entra-Portal → Benutzer → Ihr Profil → Objekt-Id. Nötig, damit nur Ihre Anrufe "
                   "erscheinen und nicht die der ganzen Firma."),
        Field("teams_user_names", "Ihre Anzeigenamen (je Zeile, Rückfall)", kind="list", optional=True,
              help="Optional, falls keine Objekt-Id: z. B. 'Mustermann, Max' und 'Max Mustermann'."),
    )
    legacy_settings = True

    def expected_errors(self) -> tuple[type[Exception], ...]:
        from .teams import TeamsError
        return (PluginError, TeamsError)

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


class TeamsLocalPlugin(EventPlugin):
    id = "teams_local"
    name = "Teams-Gespräche (lokal)"
    summary = "Teams-Anrufe und -Meetings an diesem PC – erkannt am Mikrofon, ohne Konto."
    description = ("Erkennt Teams-Anrufe und -Besprechungen daran, dass Teams auf diesem PC das Mikrofon "
                   "benutzt – ohne Entra-App, ohne Zugangsdaten, ohne Netz. Nur Gespräche an diesem PC: Was am "
                   "Handy oder Tischtelefon läuft, erscheint nicht; das kennt nur das Plugin „Microsoft Teams“. "
                   "Zeiten genau, Gesprächspartner nur, wenn Teams sie im Fenstertitel zeigt. Erfasst ab dem "
                   "Hinzufügen.")
    category = "Gespräche & Meetings"
    icon = "🎧"
    lane = "Gespräche"
    label = "Teams (lokal)"
    color = "#8b8fe8"
    records = "Beginn und Ende jeder Mikrofon-Nutzung durch Teams, dazu der Gesprächspartner, wenn Teams ihn im "
    records += "Fenstertitel zeigt."
    setup_steps = ("Plugin hinzufügen – ab dann wird jedes Teams-Gespräch an diesem PC erfasst.",)
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


class CallsLocalPlugin(EventPlugin):
    id = "calls_local"
    name = "Gespräche in allen Apps"
    summary = "Zoom, Slack, Webex, STARFACE, WhatsApp, Meetings im Browser … – erkannt am Mikrofon."
    description = ("Erkennt Gespräche und Meetings in jeder App daran, dass sie das Mikrofon benutzt – Zoom, "
                   "Slack-Huddles, Webex, Skype, Discord, WhatsApp, Signal, Telefon-Apps wie STARFACE oder 3CX und "
                   "Meetings im Browser (Google Meet, Teams im Web …). Läuft dabei auch die Kamera, gilt es als "
                   "Videogespräch. Ohne Konto und ohne Netz; erfasst ab dem Hinzufügen. Teams-Gespräche übernimmt "
                   "das Plugin „Teams-Gespräche (lokal)“, wenn es installiert ist.")
    category = "Gespräche & Meetings"
    icon = "🎙️"
    lane = "Gespräche"
    label = "Gespräch"
    color = "#e8590c"
    records = ("Welche App wann das Mikrofon benutzt hat, ob die Kamera lief, und bei Meetings im Browser der "
               "Titel des Meeting-Tabs. Keine Tonaufnahme, keine Gesprächsinhalte.")
    setup_steps = ("Plugin hinzufügen – ab dann wird jede Mikrofon-Nutzung erfasst.",
                   "Apps, die nicht erscheinen sollen (etwa ein Diktierprogramm), unten unter „Auslassen“ eintragen.")
    setting_fields = (
        Field("ignore_apps", "Auslassen (App-Namen, je Zeile)", kind="list", default=[], optional=True,
              help="Name wie im Zeitstrahl (z. B. Plaud) oder Programmdatei (z. B. dictate.exe)."),
        Field("include_other", "Auch Fernwartung, Aufnahmen und Spracheingabe erfassen", kind="bool",
              default=True, optional=True,
              help="TeamViewer, Diktier- und Aufnahme-Apps, Sprachmodus von KI-Apps. Aus: nur Gespräche."),
    )
    observes = True

    def __init__(self) -> None:
        self._recorder = None

    def _rec(self):
        if self._recorder is None:
            from .calls_local import CallsRecorder
            self._recorder = CallsRecorder()
        return self._recorder

    def unavailable_reason(self) -> str | None:
        from .calls_local import access_reason
        return access_reason()

    def test_connection(self, cfg: "Config") -> str:
        from .calls_local import describe_recent
        return self.unavailable_reason() or describe_recent()

    def observe(self, cfg: "Config", storage: "Storage", now_ms: int, *, titles_allowed: bool = True) -> int:
        s = self.settings(cfg)
        return self._rec().observe(storage, now_ms, titles_allowed=titles_allowed,
                                   ignore=s.get("ignore_apps") or [], include_other=bool(s.get("include_other")),
                                   skip_teams="teams_local" in cfg.installed_plugins)

    def on_remove(self, storage: "Storage") -> None:
        self._recorder = None


# =========================================================================== Mitteilungen

class NotificationsPlugin(EventPlugin):
    id = "notifications"
    name = "Windows-Benachrichtigungen"
    summary = "Mitteilungen von Teams, Outlook, Slack, WhatsApp … mit Absender und Vorschau."
    description = ("Schreibt die Mitteilungen mit, die Windows anzeigt – von Teams, Outlook, Slack, WhatsApp, "
                   "Signal und jeder anderen App: Absender bzw. Titel und, wenn gewünscht, die Textvorschau. "
                   "Windows behält Mitteilungen nur kurz, deshalb wird ab dem Hinzufügen laufend mitgeschrieben. "
                   "Während die Aufnahme pausiert, wird nichts gelesen.")
    category = "Mitteilungen"
    icon = "🔔"
    lane = "Mitteilungen"
    label = "Mitteilung"
    color = "#c2255c"
    records = "App, Titel (meist der Absender) und – falls eingeschaltet – die Textvorschau jeder Mitteilung."
    privacy = ("Heikel: Mitteilungen enthalten oft Nachrichten anderer Menschen. Gespeichert wird verschlüsselt; "
               "die Textvorschau lässt sich abschalten, einzelne Apps lassen sich ausschließen.")
    setup_steps = ("Plugin hinzufügen – ab dann wird jede neue Mitteilung erfasst.",
                   "Unten festlegen, ob die Textvorschau gespeichert wird und welche Apps außen vor bleiben.")
    setting_fields = (
        Field("store_text", "Textvorschau speichern", kind="bool", default=True, optional=True,
              help="Aus: nur App und Titel (meist der Absender)."),
        Field("ignore_system", "Windows-Systemmeldungen auslassen", kind="bool", default=True, optional=True),
        Field("ignore_apps", "Diese Apps auslassen (je Zeile)", kind="list", default=[], optional=True,
              help="Name wie im Zeitstrahl, z. B. WhatsApp."),
        Field("only_apps", "Nur diese Apps (je Zeile, leer = alle)", kind="list", default=[], optional=True),
    )
    observes = True

    def __init__(self) -> None:
        self._recorder = None

    def _rec(self):
        if self._recorder is None:
            from .notifications import NotificationRecorder
            self._recorder = NotificationRecorder()
        return self._recorder

    def unavailable_reason(self) -> str | None:
        from .notifications import access_reason
        return access_reason()

    def test_connection(self, cfg: "Config") -> str:
        from .notifications import describe_current
        return self.unavailable_reason() or describe_current()

    def observe(self, cfg: "Config", storage: "Storage", now_ms: int, *, titles_allowed: bool = True) -> int:
        return self._rec().observe(storage, now_ms, self.settings(cfg), titles_allowed=titles_allowed)

    def on_remove(self, storage: "Storage") -> None:
        self._recorder = None


# =========================================================================== Web

class BrowserHistoryPlugin(EventPlugin):
    id = "browser_history"
    name = "Browser-Verlauf"
    summary = "Besuchte Websites aus Edge, Chrome, Firefox … – zusammengefasst je Surf-Phase."
    description = ("Liest den Verlauf von Edge, Chrome, Brave, Vivaldi, Opera und Firefox – nur lesend, während "
                   "der Browser läuft. Statt jedes einzelnen Aufrufs erscheinen Surf-Phasen mit den meistbesuchten "
                   "Websites; die Seitentitel stehen Claude zur Verfügung. InPrivate/Inkognito landet gar nicht "
                   "erst im Verlauf.")
    category = "Web"
    icon = "🌐"
    lane = "Web"
    label = "Web"
    color = "#7950f2"
    records = "Zeitpunkt, Website und Seitentitel besuchter Seiten (±14 Tage), zusammengefasst zu Surf-Phasen."
    privacy = ("Seiten, deren Titel auf die Ausschlussliste der Aufnahme passt (etwa Online-Banking), und "
               "ausgeschlossene Domains werden nie gespeichert.")
    setup_steps = ("Plugin hinzufügen – alle gefundenen Browser-Profile werden gelesen.",
                   "Domains, die nie erscheinen sollen, unten eintragen (z. B. meine-bank.de).")
    setting_fields = (
        Field("exclude_domains", "Domains auslassen (je Zeile)", kind="list", default=[], optional=True,
              help="Gilt auch für Unterdomains: bank.de schließt online.bank.de mit aus."),
        Field("store_titles", "Seitentitel speichern", kind="bool", default=True, optional=True,
              help="Aus: nur die Websites (Domains), keine Seitentitel."),
    )

    def unavailable_reason(self) -> str | None:
        from .browser_history import find_profiles
        return None if find_profiles() else "Kein unterstützter Browser mit Verlauf gefunden."

    def test_connection(self, cfg: "Config") -> str:
        from .browser_history import describe_profiles
        return self.unavailable_reason() or describe_profiles()

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        from .browser_history import fetch_phases
        s = self.settings(ctx.cfg)
        return fetch_phases(ctx.window_start, ctx.window_end, exclude_domains=s.get("exclude_domains") or [],
                            store_titles=bool(s.get("store_titles")), title_patterns=ctx.cfg.title_patterns())


# =========================================================================== Entwicklung

class GitPlugin(EventPlugin):
    id = "git"
    name = "Git-Commits"
    summary = "Ihre Commits aus lokalen Repositories – ohne Netz und ohne Konto."
    description = ("Zeigt Ihre eigenen Commits aus den Git-Repositories in den gewählten Ordnern (Unterordner "
                   "werden bis zu drei Ebenen tief durchsucht). Eigene Commits erkennt das Plugin an Name oder "
                   "E-Mail-Adresse aus Ihrer Git-Konfiguration.")
    category = "Entwicklung"
    icon = "🌿"
    lane = "Entwicklung"
    label = "Git"
    color = "#f03e3e"
    records = "Zeitpunkt, Repository und erste Zeile der Commit-Nachricht Ihrer Commits."
    setup_steps = ("Git für Windows muss installiert sein.",
                   "Ordner mit Ihren Repositories wählen, z. B. C:\\Projekte.",
                   "Optional: weitere Namen/E-Mail-Adressen eintragen, unter denen Sie committen.")
    setting_fields = (
        Field("folders", "Ordner mit Repositories (je Zeile)", kind="folders", default=[],
              help="Unterordner werden bis zu drei Ebenen tief nach Repositories durchsucht."),
        Field("authors", "Eigene Namen oder E-Mail-Adressen (je Zeile)", kind="list", default=[], optional=True,
              help="Leer: aus der Git-Konfiguration (user.name, user.email)."),
    )

    def unavailable_reason(self) -> str | None:
        from .git_commits import find_git
        return None if find_git() else "Git ist auf diesem PC nicht installiert (git.exe nicht gefunden)."

    def check_settings(self, values: dict[str, Any]) -> None:
        from pathlib import Path
        missing = [f for f in values.get("folders") or [] if not Path(f).is_dir()]
        if missing:
            raise ValueError("Ordner nicht gefunden: " + ", ".join(missing))

    def test_connection(self, cfg: "Config") -> str:
        from .git_commits import describe_repos
        reason = self.unavailable_reason() or self.not_ready_reason(cfg)
        return f"Nicht bereit: {reason}" if reason else describe_repos(self.settings(cfg).get("folders") or [])

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        from .git_commits import fetch_commits
        s = self.settings(ctx.cfg)
        return fetch_commits(s.get("folders") or [], s.get("authors") or [], ctx.window_start, ctx.window_end)


class GitHubPlugin(EventPlugin):
    id = "github"
    name = "GitHub"
    summary = "Pushes, Pull Requests, Issues und Reviews Ihres GitHub-Kontos."
    description = ("Ihre öffentliche GitHub-Aktivität – Pushes, Pull Requests, Issues, Reviews, Kommentare, "
                   "Releases. Mit einem persönlichen Zugriffstoken auch die Aktivität in privaten Repositories. "
                   "GitHub liefert höchstens die letzten 90 Tage bzw. 300 Ereignisse.")
    category = "Entwicklung"
    icon = "🐙"
    lane = "Entwicklung"
    label = "GitHub"
    color = "#495057"
    network = "online"
    account = "token"
    records = "Art, Repository, Titel und Zeitpunkt Ihrer GitHub-Ereignisse."
    setup_steps = ("Ihren GitHub-Benutzernamen eintragen – das reicht für öffentliche Aktivität.",
                   "Für private Repositories: GitHub → Settings → Developer settings → Personal access tokens → "
                   "Token mit Leserecht auf die Repositories erzeugen und hier eintragen.")
    credential_fields = (
        Field("token", "Persönlicher Zugriffstoken (optional)", kind="secret", optional=True,
              help="Nur für Aktivität in privaten Repositories nötig."),
    )
    setting_fields = (
        Field("username", "GitHub-Benutzername", placeholder="z. B. octocat"),
    )

    def check_settings(self, values: dict[str, Any]) -> None:
        import re
        name = values.get("username") or ""
        if name and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", name):
            raise ValueError("Benutzername ungültig")

    def test_connection(self, cfg: "Config") -> str:
        from .github_activity import test_account
        reason = self.not_ready_reason(cfg)
        if reason:
            return f"Nicht bereit: {reason}"
        try:
            return test_account(self.settings(cfg)["username"], self.load_credentials().get("token") or None)
        except PluginError as e:
            return f"Fehlgeschlagen: {e}"

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        from .github_activity import fetch_activity
        return fetch_activity(self.settings(ctx.cfg)["username"], self.load_credentials().get("token") or None,
                              ctx.window_start, ctx.window_end)


# =========================================================================== PC & Netzwerk / Orte

class PcTimesPlugin(EventPlugin):
    id = "pc_times"
    name = "PC-Zeiten"
    summary = "Wann der PC an war: Einschalten, Standby, Herunterfahren."
    description = ("Liest aus dem System-Ereignisprotokoll von Windows, wann der PC eingeschaltet, schlafen gelegt, "
                   "aus dem Standby geholt und heruntergefahren wurde – auch rückwirkend und auch für Zeiten, in denen "
                   "Zeitspur nicht lief. Ohne Adminrechte, ohne Netz.")
    category = "PC & Netzwerk"
    icon = "💻"
    lane = "PC"
    label = "PC"
    color = "#868e96"
    records = "Beginn und Ende der Zeiten, in denen der PC an (nicht im Standby) war, mit Grund."
    setup_steps = ("Plugin hinzufügen – fertig.",)

    def unavailable_reason(self) -> str | None:
        from .eventlog import access_reason
        return access_reason("System")

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        from .pc_times import fetch_periods
        return fetch_periods(ctx.window_start, ctx.window_end)


class WifiPlugin(EventPlugin):
    id = "wifi"
    name = "WLAN-Netze"
    summary = "Mit welchem WLAN der PC wann verbunden war – als Ortshinweis ohne GPS."
    description = ("Liest aus dem WLAN-Protokoll von Windows, mit welchem Funknetz der PC wann verbunden war – auch "
                   "rückwirkend. Ordnen Sie Netznamen Orte zu („Firma-WLAN = Büro“), wird daraus ein Ortshinweis "
                   "ohne GPS, den auch Claude nutzt. Kabelverbindungen erscheinen nicht.")
    category = "PC & Netzwerk"
    icon = "📶"
    lane = "Orte"
    label = "WLAN"
    color = "#2f9e44"
    records = "Netzname (SSID), Beginn und Ende jeder WLAN-Verbindung und der zugeordnete Ort."
    setup_steps = ("Plugin hinzufügen.",
                   "Optional unten Orte zuordnen, je Zeile „Netzname = Ort“, z. B. „Firma-WLAN = Büro“.")
    setting_fields = (
        Field("places", "Orte zu Netznamen (je Zeile: Netzname = Ort)", kind="list", default=[], optional=True,
              placeholder="Firma-WLAN = Büro"),
    )

    def unavailable_reason(self) -> str | None:
        from .eventlog import access_reason
        from .wifi import CHANNEL
        return access_reason(CHANNEL)

    def check_settings(self, values: dict[str, Any]) -> None:
        from .wifi import parse_places
        parse_places(values.get("places") or [])

    def fetch(self, ctx: SyncContext, start: date, end: date) -> list[dict]:
        from .wifi import fetch_connections, parse_places
        return fetch_connections(ctx.window_start, ctx.window_end,
                                 parse_places(self.settings(ctx.cfg).get("places") or []))


class DawarichPlugin(EventPlugin):
    id = "dawarich"
    name = "Standort-Historie (Dawarich)"
    summary = "Aufenthalte und Fahrten aus Ihrer eigenen Dawarich-Instanz, mit Karte."
    description = ("Aufenthalte und Fahrten aus Ihrer eigenen Dawarich-Instanz, mit Karte. "
                   "Der Token wird ausschließlich über HTTPS gesendet.")
    category = "Orte"
    icon = "📍"
    lane = "Orte"
    label = "Standort"
    color = "#0f8a6a"
    network = "online"
    account = "token"
    records = "Aufenthalte (Ort, Dauer) und Fahrten (Strecke vereinfacht, Entfernung)."
    privacy = "Bewegungsdaten sind besonders schutzwürdig – sie bleiben verschlüsselt auf diesem PC."
    setup_steps = ("In Dawarich unter „Account“ den API-Token kopieren.",
                   "Basis-Adresse Ihrer Instanz (https://…) und Token hier eintragen.")
    credential_fields = (
        Field("base_url", "Basis-Adresse", placeholder="https://beispiel.example.org"),
        Field("token", "Token", kind="secret"),
    )

    def expected_errors(self) -> tuple[type[Exception], ...]:
        from .dawarich import DawarichError
        return (PluginError, DawarichError)

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


# =========================================================================== Registry

def _registry(locations: bool) -> tuple[EventPlugin, ...]:
    """Die Plugins dieser Ausgabe. Ohne Standort-Funktionen (Release-Build) fehlt Dawarich ganz."""
    plugins: list[EventPlugin] = [
        OutlookPlugin(), TeamsPlugin(), TeamsLocalPlugin(),
        IcsCalendarPlugin(), OutlookMailPlugin(), CallsLocalPlugin(), NotificationsPlugin(),
        BrowserHistoryPlugin(), GitPlugin(), GitHubPlugin(), PcTimesPlugin(), WifiPlugin(),
    ]
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
