"""Teams-Telefonie ueber Microsoft Graph (communications/callRecords).

Voraussetzung (ausserhalb dieses Programms): eine Azure-AD-App-Registrierung im eigenen Microsoft-365-Tenant mit der
Anwendungsberechtigung CallRecords.Read.All und erteiltem Admin-Consent. Der Zugriff nutzt den
Client-Credentials-Flow (App-only). Zugangsdaten (Tenant-Id, Client-Id, Client-Secret) werden per DPAPI
geschuetzt in %LOCALAPPDATA%\\Zeitspur\\teams_credentials.bin abgelegt (nicht im Klartext-config.yaml).

Netzwerkzugriff bewusst nur ueber die Standardbibliothek (urllib), damit die Dienst-EXE keine zusaetzlichen
HTTP-Abhaengigkeiten braucht.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import timeutil, winutil
from .config import data_dir

log = logging.getLogger(__name__)

SOURCE = "teams"
TEAMS_ENTROPY = b"Zeitspur-Teams-credentials-v1"   # darf sich nie aendern, sonst sind gespeicherte Zugangsdaten unlesbar
CREDENTIALS_FILE = "teams_credentials.bin"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
CALLRECORDS_URL = "https://graph.microsoft.com/v1.0/communications/callRecords"
HTTP_TIMEOUT = 30
# Graph-Antworten sind klein; Obergrenze gegen Speichererschoepfung durch kompromittierte Hosts.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
# Nur diese https-Hosts duerfen den Bearer-Token sehen; Weiterleitungen woandershin verlieren ihn.
_ALLOWED_HOSTS = {"graph.microsoft.com", "login.microsoftonline.com"}


class TeamsError(Exception):
    pass


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Folgt Weiterleitungen nur auf erlaubte https-MS-Hosts und entfernt dabei den Bearer-Token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme.lower() != "https" or parts.hostname not in _ALLOWED_HOSTS:
            raise urllib.error.HTTPError(
                req.full_url, code, f"unzulaessige Weiterleitung nach {parts.hostname}", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


_OPENER: urllib.request.OpenerDirector | None = None


def _opener() -> urllib.request.OpenerDirector:
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=winutil.https_context()), _SafeRedirect())
    return _OPENER


def _read_limited(resp) -> dict:
    raw = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TeamsError("Graph-Antwort unerwartet gross - verworfen")
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------- Zugangsdaten (DPAPI)

def credentials_path() -> Path:
    return data_dir() / CREDENTIALS_FILE


def save_credentials(tenant_id: str, client_id: str, client_secret: str, path: Path | None = None) -> Path:
    from .crypto import dpapi_protect

    payload = json.dumps({"tenant_id": tenant_id.strip(), "client_id": client_id.strip(),
                          "client_secret": client_secret}).encode("utf-8")
    p = path or credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".bin.tmp")
    tmp.write_bytes(dpapi_protect(payload, TEAMS_ENTROPY, "Zeitspur Teams credentials"))
    import os

    os.replace(tmp, p)
    return p


def load_credentials(path: Path | None = None) -> dict | None:
    from .crypto import KeyProtectionError, dpapi_unprotect

    p = path or credentials_path()
    if not p.exists():
        return None
    try:
        data = dpapi_unprotect(p.read_bytes(), TEAMS_ENTROPY)
        creds = json.loads(data.decode("utf-8"))
    except (KeyProtectionError, ValueError) as e:
        log.warning("Teams-Zugangsdaten nicht lesbar: %s", e)
        return None
    if not all(creds.get(k) for k in ("tenant_id", "client_id", "client_secret")):
        return None
    return creds


def clear_credentials(path: Path | None = None) -> bool:
    p = path or credentials_path()
    if p.exists():
        p.unlink()
        return True
    return False


def has_credentials(path: Path | None = None) -> bool:
    p = path or credentials_path()
    return p.exists()


# --------------------------------------------------------------------------- Abbildung (rein, testbar)

@dataclass
class CallEvent:
    ext_id: str | None
    ts_start: int
    ts_end: int
    subject: str
    location: str | None
    organizer: str | None
    attendees: str | None
    category: str
    extra: str | None

    def as_row(self) -> dict:
        return self.__dict__.copy()


# Eine Implementierung fuer alle Quellen (Graph wie Dawarich liefern ISO-8601).
_iso_to_ms = timeutil.iso_to_ms


IDENTITY_ROLES = ("user", "phone", "guest", "applicationInstance", "acsUser", "onPremises", "device")


def _name_tokens(name: str) -> frozenset:
    """Anzeigename -> Menge kleingeschriebener Wort-Tokens (macht 'Mustermann, Max' == 'Max Mustermann')."""
    import re

    return frozenset(t for t in re.split(r"[^0-9A-Za-zÀ-ÿ]+", (name or "").lower()) if t)


class UserMatcher:
    """Erkennt, ob eine callRecord-Identitaet der konfigurierte Nutzer ist (Objekt-Id primaer, Name als Rueckfall)."""

    def __init__(self, user_id: str = "", names=()):
        self.user_id = (user_id or "").strip().lower()
        self.name_sets = {_name_tokens(n) for n in (names or []) if n and n.strip()}

    def active(self) -> bool:
        return bool(self.user_id or self.name_sets)

    def matches(self, identity: dict | None) -> bool:
        if not identity:
            return False
        for role in IDENTITY_ROLES:
            ent = identity.get(role) or {}
            if not isinstance(ent, dict):
                continue
            if self.user_id and str(ent.get("id", "")).lower() == self.user_id:
                return True
            dn = ent.get("displayName")
            if dn and self.name_sets and _name_tokens(dn) in self.name_sets:
                return True
        return False


def _organizer_identity(record: dict) -> dict:
    """Vereinheitlicht organizer/organizer_v2 zu einem identitySet-artigen Dict."""
    ident = (record.get("organizer_v2") or {}).get("identity")
    if ident:
        return ident
    return record.get("organizer") or {}


def _participant_identity(p: dict | None) -> dict:
    """Teilnehmer -> identitySet-Dict (behandelt {'identity': {...}} und die aeltere {'user': {...}}-Form)."""
    if not p:
        return {}
    ident = p.get("identity")
    if isinstance(ident, dict):
        return ident
    return {k: v for k in IDENTITY_ROLES if isinstance((v := p.get(k)), dict)}


_GUID_RE = __import__("re").compile(
    r"^\{?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}?$", __import__("re").I)


def _is_real_name(dn: str | None, ent_id) -> bool:
    """Graph setzt bei nicht aufloesbaren Nutzern die Objekt-Id als Anzeigename - das ist kein echter Name."""
    if not dn:
        return False
    s = dn.strip()
    return bool(s) and not _GUID_RE.match(s) and s.lower() != str(ent_id or "").strip().lower()


def _identity_label(ident: dict | None) -> str | None:
    """Bester Anzeigetext einer Identitaet: echter Anzeigename einer Rolle, sonst die Telefonnummer (PSTN)."""
    if not ident:
        return None
    for role in IDENTITY_ROLES:
        ent = ident.get(role)
        if isinstance(ent, dict) and _is_real_name(ent.get("displayName"), ent.get("id")):
            return str(ent["displayName"]).strip()
    phone = ident.get("phone")
    if isinstance(phone, dict) and phone.get("id"):
        return str(phone["id"])  # Rufnummer als Rueckfall, wenn kein Name hinterlegt ist
    return None


def _display_names(participants, limit: int = 25, *, exclude=None) -> list[str]:
    """Anzeigenamen der Teilnehmer; `exclude(identitySet)->bool` blendet z. B. den Nutzer selbst aus."""
    names: list[str] = []
    for p in participants or []:
        if len(names) >= limit:
            break
        ident = _participant_identity(p)
        if exclude is not None and exclude(ident):
            continue
        name = _identity_label(ident)
        if name and name not in names:
            names.append(name)
    return names


DIRECTION_LABEL = {"outgoing": "ausgehend", "incoming": "eingehend"}


def build_call_event(record: dict, max_attendees: int = 25, *, direction: str | None = None,
                     matcher: "UserMatcher | None" = None) -> CallEvent:
    """Ein Graph-callRecord -> unser Ereignis-Schema (source='teams', category='call').

    Ist `matcher` gesetzt, wird der Nutzer selbst aus der angezeigten Gegenpart-/Teilnehmerliste
    ausgeblendet (sonst stuende bei eigenen Anrufen der eigene Name als "Gespraechspartner").
    """
    start_ms = _iso_to_ms(record.get("startDateTime"))
    end_ms = _iso_to_ms(record.get("endDateTime")) or start_ms
    organizer = _identity_label(_organizer_identity(record))
    exclude = matcher.matches if (matcher is not None and matcher.active()) else None
    # participants ist ohne $expand meist leer; participants_v2 als Fallback (falls vom Aufrufer geladen)
    attendees = _display_names(record.get("participants") or record.get("participants_v2"),
                               max_attendees, exclude=exclude)
    call_type = record.get("type") or "call"
    modalities = record.get("modalities") or []
    dir_txt = f" ({DIRECTION_LABEL[direction]})" if direction in DIRECTION_LABEL else ""
    # Bei ausgehenden Anrufen ist der Organisator der Nutzer selbst -> nie den eigenen Namen anzeigen,
    # sondern den Gegenpart (Teilnehmer ohne den Nutzer); ist keiner ermittelbar, ohne Namen.
    if direction == "outgoing":
        subject = (f"Teams-Anruf{dir_txt} mit " + ", ".join(attendees[:3])) if attendees else f"Teams-Anruf{dir_txt}"
    elif organizer:
        subject = f"Teams-Anruf{dir_txt} mit {organizer}"
    elif attendees:
        subject = f"Teams-Anruf{dir_txt} mit " + ", ".join(attendees[:3])
    else:
        subject = f"Teams-Anruf{dir_txt}"
    extra = {"type": call_type, "modalities": modalities}
    if direction:
        extra["direction"] = direction
    return CallEvent(
        ext_id=record.get("id"),
        ts_start=int(start_ms) if start_ms is not None else 0,
        ts_end=int(end_ms) if end_ms is not None else (int(start_ms) if start_ms else 0),
        subject=subject, location="Microsoft Teams", organizer=organizer,
        attendees="; ".join(attendees) or None, category="call",
        extra=json.dumps(extra, ensure_ascii=False))


# --------------------------------------------------------------------------- Graph-Zugriff

class TeamsCallRecords:
    def __init__(self, credentials: dict, *, max_attendees: int = 25):
        self.tenant_id = credentials["tenant_id"]
        self.client_id = credentials["client_id"]
        self.client_secret = credentials["client_secret"]
        self.max_attendees = max_attendees
        self._token: str | None = None

    @classmethod
    def from_stored(cls, path: Path | None = None) -> "TeamsCallRecords | None":
        creds = load_credentials(path)
        return cls(creds) if creds else None

    def _post(self, url: str, data: dict) -> dict:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        # Zertifikate pruefen wie Windows (auch auf frischen PCs, siehe winutil.https_context); sichere Redirects.
        with _opener().open(req, timeout=HTTP_TIMEOUT) as resp:
            return _read_limited(resp)

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, method="GET",
                                     headers={"Authorization": f"Bearer {self._require_token()}",
                                              "Accept": "application/json"})
        with _opener().open(req, timeout=HTTP_TIMEOUT) as resp:
            return _read_limited(resp)

    def acquire_token(self) -> str:
        url = TOKEN_URL.format(tenant=urllib.parse.quote(self.tenant_id))
        try:
            result = self._post(url, {"grant_type": "client_credentials", "client_id": self.client_id,
                                      "client_secret": self.client_secret, "scope": GRAPH_SCOPE})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise TeamsError(f"Token-Anforderung fehlgeschlagen ({e.code}): {detail}") from e
        except urllib.error.URLError as e:
            raise TeamsError(f"Keine Verbindung zu Microsoft Login: {e.reason}") from e
        token = result.get("access_token")
        if not token:
            raise TeamsError(f"Kein access_token erhalten: {result.get('error_description') or result}")
        self._token = token
        return token

    def _require_token(self) -> str:
        return self._token or self.acquire_token()

    def test_connection(self) -> str:
        """Fordert ein Token an und liest eine Seite callRecords - zum Pruefen der Einrichtung."""
        self.acquire_token()
        today = date.today()
        self.fetch(today, today)
        return "OK"

    def _expand_participants(self, ext_id: str) -> list[dict]:
        """Teilnehmer eines Anrufs per Einzelabruf (die Sammelabfrage liefert sie nicht mit)."""
        url = CALLRECORDS_URL + "/" + urllib.parse.quote(ext_id) + "?$expand=participants_v2"
        try:
            rec = self._get(url)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            log.debug("callRecord %s nicht expandierbar: %s", ext_id, e)
            return []
        return rec.get("participants_v2") or rec.get("participants") or []

    def fetch(self, start: date, end: date, *, matcher: "UserMatcher | None" = None,
              checked: dict | None = None) -> list[CallEvent]:
        """callRecords im lokalen Fenster [start, end], beschnitten auf die von Graph erlaubten letzten 30 Tage.

        Ist `matcher` aktiv, werden nur Anrufe des Nutzers zurueckgegeben (Organisator = ausgehend,
        Teilnehmer = eingehend; Teilnehmer per Einzelabruf). `checked` ist ein persistenter Cache
        {ext_id: bool}, den fetch liest und um neu klassifizierte Anrufe ergaenzt (callRecords sind
        unveraenderlich, daher genuegt eine einmalige Klassifizierung).
        """
        now = datetime.now(timezone.utc)
        floor = now - timedelta(days=30)
        start_utc = max(datetime.combine(start, time.min).astimezone().astimezone(timezone.utc), floor)
        end_utc = min((datetime.combine(end, time.min) + timedelta(days=1)).astimezone().astimezone(timezone.utc), now)
        if end_utc <= start_utc:
            return []
        flt = (f"startDateTime ge {start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} "
               f"and startDateTime lt {end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        url = CALLRECORDS_URL + "?" + urllib.parse.urlencode({"$filter": flt})
        filtering = matcher is not None and matcher.active()
        events: list[CallEvent] = []
        pages = 0
        while url and pages < 50:
            pages += 1
            try:
                data = self._get(url)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                raise TeamsError(f"callRecords-Abruf fehlgeschlagen ({e.code}): {detail}") from e
            except urllib.error.URLError as e:
                raise TeamsError(f"Keine Verbindung zu Microsoft Graph: {e.reason}") from e
            for record in data.get("value", []):
                ev = self._classify(record, matcher, checked) if filtering else build_call_event(record, self.max_attendees)
                if ev is not None and ev.ts_start:
                    events.append(ev)
            next_url = data.get("@odata.nextLink")
            if next_url:
                parts = urllib.parse.urlsplit(next_url)
                # Nur einer https-Fortsetzung auf graph.microsoft.com folgen - kein Bearer-Token an fremde Hosts.
                if parts.scheme != "https" or parts.hostname != "graph.microsoft.com":
                    log.warning("callRecords: unerwarteter nextLink-Host verworfen: %s", parts.hostname)
                    break
            url = next_url
        log.info("Teams: %d Anrufe fuer %s..%s gelesen%s", len(events), start, end,
                 " (auf Nutzer gefiltert)" if filtering else "")
        return events

    def _classify(self, record: dict, matcher: "UserMatcher", checked: dict | None) -> "CallEvent | None":
        """Gibt einen CallEvent zurueck, wenn der Nutzer an diesem Anruf beteiligt war, sonst None."""
        ext_id = record.get("id")
        # 1) Organisator kommt frei aus der Sammelabfrage -> ausgehender Anruf des Nutzers
        if matcher.matches(_organizer_identity(record)):
            if ext_id and checked is not None:
                checked[ext_id] = True
            record["participants_v2"] = self._expand_participants(ext_id) if ext_id else []
            return build_call_event(record, self.max_attendees, direction="outgoing", matcher=matcher)
        # 2) sonst Teilnehmer pruefen (eingehend) - Ergebnis cachen
        cached = checked.get(ext_id) if (checked is not None and ext_id) else None
        if cached is False:
            return None
        if cached is True:
            record["participants_v2"] = self._expand_participants(ext_id)
            return build_call_event(record, self.max_attendees, direction="incoming", matcher=matcher)
        parts = self._expand_participants(ext_id) if ext_id else []
        involved = any(matcher.matches(_participant_identity(p)) for p in parts)
        if ext_id and checked is not None:
            checked[ext_id] = involved
        if not involved:
            return None
        record["participants_v2"] = parts
        return build_call_event(record, self.max_attendees, direction="incoming", matcher=matcher)
