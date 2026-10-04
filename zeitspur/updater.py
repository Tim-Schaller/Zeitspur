"""Automatische Updates aus der oeffentlichen Release-Quelle (GitHub-Releases).

Ablauf: Einige Minuten nach dem Start und danach alle 12 Stunden holt Zeitspur die Datei latest.json des
neuesten Releases. Sie nennt Version, Setup-Datei, Groesse und SHA-256 und ist mit dem Release-Schluessel
signiert (Ed25519). Ist die Version neuer, wird das Setup geladen und geprueft. Sobald der PC eine Weile
(update_idle_minutes, Standard 5) nicht benutzt wird, laeuft es still durch: Es beendet Zeitspur, ersetzt die
Programmdateien und startet es wieder (installer.iss, Parameter /UPDATE=1). Sind automatische Updates aus,
erscheint nur ein Hinweis.

Sicherheit: Installiert wird nur, was mit dem privaten Release-Schluessel signiert ist. Der liegt allein beim
Herausgeber (tools/release_key.py); im Programm steckt nur der oeffentliche Teil. Wer die Download-Quelle oder
das GitHub-Konto uebernimmt, kann deshalb trotzdem keinen fremden Code verteilen. Eine aeltere oder gleiche
Version wird nie installiert - auch kein altes, gueltig signiertes Setup.

Nur Release-Builds aktualisieren sich selbst (edition.UPDATE_CHANNEL). Der eigene Build und der Quelltextbetrieb
nicht: Dort koennte ein Release Funktionen ersetzen, an denen gerade gearbeitet wird.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__, winutil

log = logging.getLogger(__name__)

REPO = "Tim-Schaller/Zeitspur"
MANIFEST_NAME = "latest.json"
MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/{MANIFEST_NAME}"
# Oeffentlicher Teil des Release-Schluessels (Ed25519, roh, Base64) - erzeugt mit tools/release_key.py init.
PUBLIC_KEY = "7dOB4noryKsX1e/VfXwYL+nXgSxc5y9Pxms3KgZtQsM="
# Eine Signatur gilt nur fuer diesen Zweck. Unveraenderlich - sonst lehnen installierte Versionen jedes Update ab.
SIGNATURE_CONTEXT = b"Zeitspur-update-v1\n"

INITIAL_DELAY_S = 300.0          # erste Pruefung 5 min nach dem Start - der Start hat genug zu tun
INTERVAL_S = 12 * 3600.0
# Wie lange der PC unbenutzt sein muss, bevor installiert wird, steht in der Konfiguration (update_idle_minutes)
TICK_S = 15.0
FAILED_COOLDOWN_S = 24 * 3600.0  # nach einem gescheiterten Update dieselbe Version so lange nicht automatisch
INSTALL_TIMEOUT_S = 15 * 60.0    # bis dahin haette das Setup Zeitspur laengst beendet
NETWORK_TIMEOUT_S = 30.0
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SETUP_BYTES = 500 * 1024 * 1024
MAX_NOTES_CHARS = 4000

# Fuer Tests (Windows Sandbox): eigener Kanal, etwa file:///C:/ZeitspurTest/channel/latest.json, und kurze
# Zeiten "Start,Intervall[,Leerlauf]" in Sekunden - ohne Leerlauf gilt die Einstellung. Die Signatur bleibt Pflicht.
ENV_URL = "ZEITSPUR_UPDATE_URL"
ENV_TIMING = "ZEITSPUR_UPDATE_TIMING"

_VERSION_RE = re.compile(r"\d{1,5}(\.\d{1,5}){1,3}")
_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.exe")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW: kein aufblitzendes Konsolenfenster fuer cmd


class UpdateError(Exception):
    """Update-Quelle nicht erreichbar, Manifest ungueltig, Signatur oder Pruefsumme falsch."""


# --------------------------------------------------------------------------- Versionen, Manifest, Signatur

def parse_version(text: Any) -> tuple[int, ...]:
    if not isinstance(text, str) or not _VERSION_RE.fullmatch(text):
        raise UpdateError(f"Versionsnummer ungueltig: {text!r}")
    return tuple(int(teil) for teil in text.split("."))


def release_page(version: Any) -> str | None:
    """Seite der Version auf GitHub (Release-Notizen) - nur aus Repository und gepruefter Versionsnummer gebaut."""
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        return None
    return f"https://github.com/{REPO}/releases/tag/v{version}"


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def signed_bytes(payload: dict[str, Any]) -> bytes:
    """Kanonische Form des Manifests, ueber die signiert wird: sortierte Schluessel, kein Leerraum."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return SIGNATURE_CONTEXT + text.encode("utf-8")


def sign_payload(payload: dict[str, Any], private_key) -> str:
    """Signiert ein Manifest (tools/release_key.py, Tests). private_key: Ed25519PrivateKey."""
    return base64.b64encode(private_key.sign(signed_bytes(payload))).decode("ascii")


def verify_manifest(raw: bytes, public_key: str = PUBLIC_KEY) -> dict[str, Any]:
    """Prueft Signatur und Felder eines latest.json und liefert den signierten Inhalt. Wirft UpdateError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("Manifest zu gross")
    try:
        doc = json.loads(raw.decode("utf-8"))
        payload = doc["payload"]
        signature = base64.b64decode(doc["signature"], validate=True)
    except (ValueError, KeyError, TypeError) as e:
        raise UpdateError(f"Manifest unlesbar: {e}") from e
    if not isinstance(payload, dict):
        raise UpdateError("Manifest unlesbar: payload fehlt")
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(signature, signed_bytes(payload))
    except (InvalidSignature, ValueError) as e:
        raise UpdateError("Signatur ungueltig - dieses Update wird nicht installiert") from e
    _check_payload(payload)
    return payload


def _check_payload(p: dict[str, Any]) -> None:
    parse_version(p.get("version"))
    if not isinstance(p.get("file"), str) or not _FILE_RE.fullmatch(p["file"]):
        raise UpdateError(f"Dateiname im Manifest ungueltig: {p.get('file')!r}")
    if not isinstance(p.get("sha256"), str) or not _SHA_RE.fullmatch(p["sha256"]):
        raise UpdateError("Pruefsumme im Manifest ungueltig")
    size = p.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_SETUP_BYTES:
        raise UpdateError(f"Dateigroesse im Manifest ungueltig: {size!r}")
    if not isinstance(p.get("notes", ""), str) or len(p.get("notes", "")) > MAX_NOTES_CHARS:
        raise UpdateError("Aenderungsliste im Manifest ungueltig")
    url = p.get("url")
    if url is not None and (not isinstance(url, str) or not url.startswith("https://")):
        raise UpdateError("Download-Adresse im Manifest muss https sein")


def setup_url(payload: dict[str, Any], manifest_url: str) -> str:
    """Download-Adresse: aus dem Manifest, sonst die Datei neben latest.json (Testkanal)."""
    return payload.get("url") or urllib.parse.urljoin(manifest_url, payload["file"])


# --------------------------------------------------------------------------- Netz und Dateien

class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Folgt nur Weiterleitungen, die wieder auf https zeigen - kein Downgrade auf http/ftp."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.HTTPError(
                newurl, code, "Weiterleitung auf Nicht-https-Adresse abgelehnt", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str):
    if urllib.parse.urlsplit(url).scheme.lower() not in ("https", "file"):
        raise UpdateError(f"Nur https-Adressen sind erlaubt: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": f"Zeitspur/{__version__}"})
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=winutil.https_context()), _HttpsOnlyRedirect())
    try:
        return opener.open(request, timeout=NETWORK_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        raise UpdateError(f"Update-Quelle antwortet mit HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise UpdateError(f"Zertifikat der Update-Quelle nicht vertrauenswürdig ({reason.verify_message})") from e
        raise UpdateError(f"Update-Quelle nicht erreichbar: {reason}") from e


def fetch_bytes(url: str, limit: int = MAX_MANIFEST_BYTES) -> bytes:
    with _open(url) as response:
        try:
            data = response.read(limit + 1)
        except OSError as e:
            raise UpdateError(f"Update-Quelle nicht lesbar: {e}") from e
    if len(data) > limit:
        raise UpdateError("Antwort der Update-Quelle zu gross")
    return data


def file_matches(path: Path, sha256: str, size: int) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest() == sha256
    except OSError:
        return False


def download(url: str, dest: Path, sha256: str, size: int, progress: Callable[[float], None] | None = None) -> None:
    """Laedt nach dest (ueber eine .part-Datei) und prueft Groesse und SHA-256. Wirft UpdateError."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest, received = hashlib.sha256(), 0
    try:
        with _open(url) as response, open(part, "wb") as f:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                received += len(chunk)
                if received > size:
                    raise UpdateError("Setup ist groesser als angekuendigt")
                digest.update(chunk)
                f.write(chunk)
                if progress is not None:
                    progress(received / size)
        if received != size or digest.hexdigest() != sha256:
            raise UpdateError("Pruefsumme des Setups stimmt nicht - Download verworfen")
        os.replace(part, dest)
    except OSError as e:
        raise UpdateError(f"Download fehlgeschlagen: {e}") from e
    finally:
        part.unlink(missing_ok=True)


def setup_command(setup: Path, log_file: Path) -> str:
    """Kommandozeile fuer das Update-Setup.

    Gestartet ueber "cmd /c start": So haengt das Setup nicht im Prozessbaum von Zeitspur. Das Setup beendet
    Zeitspur samt Prozessbaum (taskkill /T, damit auch Tesseract und WebView2 die Dateien freigeben) - als Kind
    von Zeitspur hat es sich dabei frueher selbst mitbeendet, und Zeitspur blieb einfach weg (Sandbox-Test
    04.10.2026). /SILENT zeigt ein kleines Fortschrittsfenster, damit niemand im Dunkeln sitzt; Rueckfragen gibt es
    keine (/SUPPRESSMSGBOXES)."""
    comspec = os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
    return (f'"{comspec}" /d /c start "" /D "{setup.parent}" "{setup}" '
            f'/SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE=1 "/LOG={log_file}"')


def launch_setup(setup: Path, log_file: Path) -> None:
    """Startet das Setup - es beendet Zeitspur, ersetzt die Dateien und startet Zeitspur wieder."""
    subprocess.Popen(setup_command(setup, log_file), close_fds=True, creationflags=_NO_WINDOW)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


FLOOR_NAME = "floor.json"


def read_floor(folder: Path, default: str) -> str:
    """Hoechste je gesehene/installierte Version als Untergrenze (Anti-Rollback), mind. `default`."""
    info = _read_json(folder / FLOOR_NAME)
    cand = info.get("version") if info else None
    try:
        return cand if cand and is_newer(cand, default) else default
    except UpdateError:
        return default


def raise_floor(folder: Path, version: str) -> None:
    """Hebt den Versions-Boden an (nie senken)."""
    try:
        parse_version(version)
    except UpdateError:
        return
    current = read_floor(folder, version)
    if is_newer(version, current) or version == current:
        _write_json(folder / FLOOR_NAME, {"version": version if is_newer(version, current) else current})


def _remove_setups(folder: Path, keep: Path | None = None) -> None:
    for old in folder.glob("*.exe*"):
        if keep is None or old != keep:
            old.unlink(missing_ok=True)


def mark_failed(folder: Path, version: str, cooldown_s: float = FAILED_COOLDOWN_S) -> None:
    _write_json(folder / "failed.json", {"version": version, "until": time.time() + cooldown_s})


def in_cooldown(folder: Path, version: str) -> bool:
    info = _read_json(folder / "failed.json")
    return bool(info and info.get("version") == version and time.time() < float(info.get("until", 0)))


def read_pending(data_dir: Path) -> dict[str, Any] | None:
    """Das gerade installierte Update (pending.json), ohne es zu verbrauchen - fuer das Startfenster danach."""
    info = _read_json(data_dir / "updates" / "pending.json")
    return info if info and isinstance(info.get("to"), str) else None


def consume_pending(data_dir: Path, current: str = __version__) -> tuple[str, str, bool] | None:
    """Nach einem Update-Versuch beim naechsten Start: ("ok", Version, Fenster zeigen?) oder ("failed", Ziel, ...)."""
    folder = data_dir / "updates"
    marker = folder / "pending.json"
    info = _read_json(marker)
    if info is None:
        return None
    marker.unlink(missing_ok=True)
    target, show = str(info.get("to", "")), bool(info.get("show"))
    try:
        arrived = not is_newer(target, current)
    except UpdateError:
        return None
    if arrived:
        _remove_setups(folder)
        (folder / "failed.json").unlink(missing_ok=True)
        raise_floor(folder, current)   # Boden endgueltig auf die tatsaechlich installierte Version ziehen
        return ("ok", current, show)
    mark_failed(folder, target)
    return ("failed", target, show)


def _env_timing() -> tuple[float, float, float | None] | None:
    raw = os.environ.get(ENV_TIMING, "")
    try:
        values = [float(v) for v in raw.split(",")]
    except ValueError:
        return None
    if len(values) == 2:
        return values[0], values[1], None
    return (values[0], values[1], values[2]) if len(values) == 3 else None


def describe_idle(seconds: float) -> str:
    """'5 Minuten', '1 Minute', '30 Sekunden' - fuer Meldungen."""
    if seconds < 60:
        return f"{round(seconds)} Sekunden"
    minutes = round(seconds / 60)
    return "1 Minute" if minutes == 1 else f"{minutes} Minuten"


# --------------------------------------------------------------------------- Hintergrund-Thread

@dataclass
class UpdateState:
    status: str = "idle"   # idle, checking, current, available, downloading, ready, installing, error
    current: str = __version__
    version: str = ""      # angebotene neue Version
    notes: str = ""
    error: str = ""
    progress: float = 0.0
    checked_ms: int = 0


class Updater:
    """Prueft, laedt und installiert Updates. Aus beliebigen Threads bedienbar (check_now, install_now)."""

    def __init__(self, cfg, data_dir: Path, stop_event: threading.Event, *,
                 notify: Callable[[str], None] | None = None, manifest_url: str | None = None,
                 public_key: str = PUBLIC_KEY, current: str = __version__,
                 idle_seconds: Callable[[], float] = winutil.idle_seconds,
                 launch: Callable[[Path, Path], None] = launch_setup,
                 window_visible: Callable[[], bool] = lambda: False,
                 timing: tuple[float, float, float | None] | None = None, tick_s: float = TICK_S):
        self.cfg = cfg                      # lebende Konfiguration: auto_update und Leerlaufzeit gelten sofort
        self.folder = data_dir / "updates"
        self.manifest_url = manifest_url or os.environ.get(ENV_URL) or MANIFEST_URL
        self.delay_s, self.interval_s, self._idle_override = timing or _env_timing() or (
            INITIAL_DELAY_S, INTERVAL_S, None)
        self.public_key = public_key
        self.current = current
        self._notify = notify or (lambda _msg: None)
        self._idle_seconds = idle_seconds
        self._launch = launch
        self._window_visible = window_visible
        self._tick_s = tick_s
        self._stop = stop_event
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._state = UpdateState(current=current)
        self._payload: dict[str, Any] | None = None
        self._setup: Path | None = None
        self._check_requested = False
        self._manual_check = False
        self._install_requested = False
        self._installing_since: float | None = None
        self._announced: set[str] = set()
        self._thread = threading.Thread(target=self._run, name="updater", daemon=True)

    # ---- von aussen ------------------------------------------------------------------------------
    def start(self) -> None:
        log.info("Updates: Quelle %s, automatisch %s", self.manifest_url, "an" if self.cfg.auto_update else "aus")
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def check_now(self, manual: bool = True) -> None:
        self._check_requested, self._manual_check = True, manual
        self._wake.set()

    def install_now(self) -> None:
        """Sofort installieren, ohne auf den Leerlauf zu warten (Knopf "Jetzt aktualisieren")."""
        self._install_requested = True
        if self._payload is None:
            self._check_requested = True
        self._wake.set()

    def idle_needed_s(self) -> float:
        """Leerlauf vor der Installation: aus der Einstellung, ausser ein Test gibt ihn vor."""
        if self._idle_override is not None:
            return self._idle_override
        return max(1, int(getattr(self.cfg, "update_idle_minutes", 5))) * 60.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = asdict(self._state)
        data["auto"] = bool(self.cfg.auto_update)
        data["idle_s"] = self.idle_needed_s()
        data["release_url"] = release_page(data["version"])
        return data

    # ---- Ablauf ----------------------------------------------------------------------------------
    def _set(self, **changes: Any) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(self._state, name, value)

    @property
    def status(self) -> str:
        with self._lock:
            return self._state.status

    def _run(self) -> None:
        next_check = time.monotonic() + self.delay_s
        while not self._stop.is_set():
            due = time.monotonic() >= next_check
            if due:
                next_check = time.monotonic() + self.interval_s
            try:
                self.step(due)
            except Exception:
                log.exception("Update: unerwarteter Fehler")
            self._wake.wait(self._tick_s)
            self._wake.clear()

    def step(self, check_due: bool = False) -> None:
        """Ein Durchgang: ggf. pruefen, laden, im Leerlauf installieren. Fuer Tests einzeln aufrufbar."""
        if check_due or self._check_requested:
            manual, self._check_requested, self._manual_check = self._manual_check, False, False
            self.check(manual=manual)
        if self._install_requested and self.status in ("available", "error"):
            self.fetch_setup()
        if self._should_install():
            self.install()
        self._watch_install()

    def check(self, manual: bool = False) -> None:
        before = self.status
        if before in ("downloading", "installing"):
            return
        self._set(status="checking", error="")
        try:
            payload = verify_manifest(fetch_bytes(self.manifest_url), self.public_key)
        except UpdateError as e:
            log.warning("Update-Pruefung: %s", e)
            known = before in ("available", "ready")
            # Ein kurzer Netzaussetzer soll ein bereits gefundenes Update nicht vergessen lassen
            self._set(status=before if known else "error", error=str(e), checked_ms=int(time.time() * 1000))
            if not known:
                self._install_requested = False
            if manual:
                self._notify(f"Update-Prüfung fehlgeschlagen: {e}")
            return
        now_ms = int(time.time() * 1000)
        # Gegen Rollback: nur installieren, was echt neuer ist als die hoechste je gesehene Version,
        # nicht nur neuer als die gerade laufende. Ein erneut als "latest" ausgeliefertes altes
        # (gueltig signiertes) Manifest wird so abgelehnt.
        floor = read_floor(self.folder, self.current)
        if not is_newer(payload["version"], floor):
            self._payload, self._setup = None, None
            self._set(status="current", version="", notes="", checked_ms=now_ms)
            log.info("Update-Pruefung: %s ist aktuell (Boden %s)", self.current, floor)
            if manual:
                self._notify(f"Zeitspur ist auf dem neuesten Stand ({self.current}).")
            self._install_requested = False
            return
        known = self._payload is not None and self._payload["version"] == payload["version"] and before == "ready"
        self._payload = payload
        raise_floor(self.folder, payload["version"])
        if known:
            self._set(status="ready", checked_ms=now_ms)
        else:
            self._setup = None
            self._set(status="available", version=payload["version"], notes=payload.get("notes", ""),
                      progress=0.0, checked_ms=now_ms)
            log.info("Update verfuegbar: %s -> %s", self.current, payload["version"])
        self._announce(payload["version"], again=manual)
        if self.cfg.auto_update or self._install_requested:
            self.fetch_setup()

    def fetch_setup(self) -> None:
        payload = self._payload
        if payload is None or self.status not in ("available", "error"):
            return
        dest = self.folder / payload["file"]
        if not file_matches(dest, payload["sha256"], payload["size"]):
            self._set(status="downloading", progress=0.0, error="")
            try:
                download(setup_url(payload, self.manifest_url), dest, payload["sha256"], payload["size"],
                         progress=lambda frac: self._set(progress=round(frac, 3)))
            except UpdateError as e:
                log.warning("Update %s: %s", payload["version"], e)
                self._set(status="available", error=str(e))
                if self._install_requested:
                    self._install_requested = False
                    self._notify(f"Update konnte nicht geladen werden: {e}")
                return
        self._setup = dest
        _remove_setups(self.folder, keep=dest)
        self._set(status="ready", progress=1.0, error="")
        log.info("Update %s geladen und geprueft", payload["version"])

    def _should_install(self) -> bool:
        if self.status != "ready" or self._setup is None or self._payload is None:
            return False
        if self._install_requested:
            return True
        if not self.cfg.auto_update or in_cooldown(self.folder, self._payload["version"]):
            return False
        return self._idle_seconds() >= self.idle_needed_s()

    def install(self) -> None:
        payload, setup = self._payload, self._setup
        manual, self._install_requested = self._install_requested, False
        if payload is None or setup is None:
            return
        if not file_matches(setup, payload["sha256"], payload["size"]):   # seit dem Laden veraendert?
            log.warning("Update %s: Setup-Datei hat sich seit dem Laden veraendert - wird neu geladen",
                        payload["version"])
            self._setup = None
            self._set(status="available")
            self.fetch_setup()
            return
        # show: nach dem Neustart wieder so wie vorher - das Fenster kommt zurueck, wenn es offen war oder jemand
        # selbst auf "Jetzt installieren" geklickt hat; sonst startet Zeitspur wieder im Infobereich.
        show = manual or bool(self._window_visible())
        _write_json(self.folder / "pending.json", {"from": self.current, "to": payload["version"],
                                                   "ts": int(time.time()), "show": show})
        log.info("Installiere Update %s -> %s", self.current, payload["version"])
        try:
            self._launch(setup, self.folder / f"setup-{payload['version']}.log")
        except OSError as e:
            (self.folder / "pending.json").unlink(missing_ok=True)
            log.error("Update-Setup liess sich nicht starten: %s", e)
            self._set(status="error", error=f"Setup ließ sich nicht starten: {e}")
            return
        self._installing_since = time.monotonic()
        self._set(status="installing")

    def _watch_install(self) -> None:
        """Beendet das Setup Zeitspur nicht, ist es gescheitert - dann nicht endlos weiter versuchen."""
        if self._installing_since is None or time.monotonic() - self._installing_since < INSTALL_TIMEOUT_S:
            return
        self._installing_since = None
        (self.folder / "pending.json").unlink(missing_ok=True)
        version = self._payload["version"] if self._payload else ""
        mark_failed(self.folder, version)
        log.error("Update auf %s: Das Setup hat Zeitspur nicht beendet - abgebrochen", version)
        self._set(status="error", error="Das Setup ist nicht durchgelaufen (Protokoll im Ordner updates)")

    def _announce(self, version: str, again: bool = False) -> None:
        """Einmal je Version Bescheid geben - und immer, wenn jemand selbst nachgefragt hat."""
        if version in self._announced and not again:
            return
        self._announced.add(version)
        if self.cfg.auto_update:
            self._notify(f"Zeitspur {version} ist verfügbar und wird installiert, sobald der PC "
                         f"{describe_idle(self.idle_needed_s())} nicht benutzt wird.")
        else:
            self._notify(f"Zeitspur {version} ist verfügbar – im Zeitstrahl über „Jetzt aktualisieren“.")
