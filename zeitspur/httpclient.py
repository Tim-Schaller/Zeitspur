"""Gemeinsamer HTTPS-Zugang der Online-Plugins (Kalender per ICS-Link, GitHub).

Regeln, die hier fuer alle gelten:
- Nur https (webcal:// wird zu https://). Zertifikate werden immer geprueft, wie Windows es tut (truststore).
- Weiterleitungen nur auf https. Wechselt dabei der Host, faellt der Authorization-Header weg - urllib wuerde
  ihn sonst an den fremden Host mitschicken.
- Zugangsdaten stehen nur im Header, nie in Meldungen: Fehlertexte nennen hoechstens den Host. Geheime
  Kalender-Links stecken komplett in der Adresse und duerfen deshalb nirgends auftauchen.
- Antwortgroesse begrenzt, Zeitlimit, bei 429/503 kurz warten (Retry-After) und hoechstens zweimal wiederholen.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import __version__, winutil

log = logging.getLogger(__name__)

TIMEOUT_S = 30
MAX_BYTES = 10 * 1024 * 1024
MAX_RETRIES = 2
MAX_RETRY_WAIT_S = 30


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def normalize_url(url: str) -> str:
    """Bereinigt eine eingegebene Adresse; webcal:// wird zu https://. Alles ausser https wird abgelehnt."""
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("Nur https-Adressen sind erlaubt (sonst wäre der Inhalt unterwegs mitlesbar).")
    return url


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "?"


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        target = urllib.parse.urlsplit(new.full_url)
        if target.scheme.lower() != "https":
            raise HttpError("Weiterleitung auf eine unverschlüsselte Adresse abgelehnt.", code)
        if target.hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=winutil.https_context()), _SafeRedirect())


def _message(status: int) -> str:
    if status in (401, 403):
        return f"Zugriff verweigert (HTTP {status}) - Zugangsdaten bzw. Link prüfen."
    if status == 404:
        return "Nicht gefunden (HTTP 404) - Adresse bzw. Name prüfen."
    if status == 429:
        return "Zu viele Anfragen (HTTP 429) - später erneut versuchen."
    if status >= 500:
        return f"Der Dienst hat ein Problem (HTTP {status}) - später erneut versuchen."
    return f"Unerwartete Antwort (HTTP {status})."


def _retry_wait(err: urllib.error.HTTPError) -> float | None:
    if err.code not in (429, 503):
        return None
    try:
        wait = float(err.headers.get("Retry-After", "5"))
    except (TypeError, ValueError):
        wait = 5.0
    return wait if 0 <= wait <= MAX_RETRY_WAIT_S else None


def request(url: str, *, headers: dict[str, str] | None = None, timeout: float = TIMEOUT_S,
            max_bytes: int = MAX_BYTES, opener: urllib.request.OpenerDirector | None = None) -> bytes:
    """GET; liefert den Inhalt. Wirft HttpError mit einer Meldung, die keine Adresse und kein Geheimnis enthaelt."""
    url = normalize_url(url)
    host = host_of(url)
    all_headers = {"User-Agent": f"Zeitspur/{__version__}", **(headers or {})}
    opener = opener or _opener()
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, method="GET", headers=all_headers)
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise HttpError(f"Antwort von {host} ist zu groß (über {max_bytes // (1024 * 1024)} MB).")
            return body
        except urllib.error.HTTPError as e:
            wait = _retry_wait(e)
            if wait is not None and attempt < MAX_RETRIES:
                log.info("%s: HTTP %d, neuer Versuch in %.0f s", host, e.code, wait)
                time.sleep(wait)
                continue
            raise HttpError(_message(e.code), e.code) from None
        except urllib.error.URLError as e:
            reason = e.reason if isinstance(e.reason, str) else type(e.reason).__name__
            if isinstance(e.reason, OSError) and getattr(e.reason, "strerror", None):
                reason = e.reason.strerror
            raise HttpError(f"{host} nicht erreichbar: {reason}") from None
        except TimeoutError:
            raise HttpError(f"{host} antwortet nicht (Zeitüberschreitung).") from None
    raise HttpError(f"{host}: zu viele Anfragen.")  # pragma: no cover - Schleife endet vorher


def get_json(url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
             **kwargs) -> Any:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = request(url, headers={"Accept": "application/json", **(headers or {})}, **kwargs)
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise HttpError(f"{host_of(url)} hat kein gültiges JSON geliefert.") from None
