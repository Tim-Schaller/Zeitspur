"""Schluesselverwaltung (Windows DPAPI) und Oeffnen der SQLCipher-Datenbank.

Der 256-Bit-Datenbankschluessel wird beim ersten Start zufaellig erzeugt und mit
CryptProtectData an das Windows-Benutzerkonto gebunden (Scope: aktueller Benutzer,
kein LOCAL_MACHINE). Nur Prozesse unter demselben Konto koennen key.bin entschluesseln.
"""
from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path

import sqlcipher3

log = logging.getLogger(__name__)

KEY_BYTES = 32
KEY_DESCRIPTION = "Zeitspur DB key"
# Zusaetzliche Entropie: bindet den Blob an diese Anwendung (Defense in depth, kein Geheimnis).
ENTROPY = b"Zeitspur-DPAPI-v1"   # darf sich nie aendern, sonst ist key.bin unlesbar
CRYPTPROTECT_UI_FORBIDDEN = 0x01

BUSY_TIMEOUT_MS = 5000
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024


class KeyProtectionError(Exception):
    """key.bin fehlt nicht, kann aber nicht entschluesselt werden (anderes Konto, manipuliert, DPAPI-Masterkey verloren)."""


class WrongKeyError(Exception):
    """Die Datenbank laesst sich mit dem vorhandenen Schluessel nicht lesen."""


# --------------------------------------------------------------------------- DPAPI

def _win32crypt():
    if sys.platform != "win32":  # pragma: no cover
        raise KeyProtectionError("DPAPI ist nur unter Windows verfuegbar")
    import win32crypt  # noqa: WPS433 - bewusst spaet importiert

    return win32crypt


def dpapi_protect(data: bytes, entropy: bytes = ENTROPY, description: str = KEY_DESCRIPTION) -> bytes:
    """Schuetzt beliebige Bytes per DPAPI (Scope: aktueller Benutzer)."""
    return bytes(_win32crypt().CryptProtectData(data, description, entropy, None, None, CRYPTPROTECT_UI_FORBIDDEN))


def dpapi_unprotect(blob: bytes, entropy: bytes = ENTROPY) -> bytes:
    try:
        _desc, data = _win32crypt().CryptUnprotectData(blob, entropy, None, None, CRYPTPROTECT_UI_FORBIDDEN)
    except Exception as e:  # pywintypes.error
        raise KeyProtectionError(f"DPAPI-Entschluesselung fehlgeschlagen: {e}") from e
    return bytes(data)


def protect_key(key: bytes) -> bytes:
    """Verschluesselt den Rohschluessel per DPAPI (aktueller Benutzer)."""
    if len(key) != KEY_BYTES:
        raise ValueError("Schluessel muss 32 Byte lang sein")
    return dpapi_protect(key)


def unprotect_key(blob: bytes) -> bytes:
    data = dpapi_unprotect(blob)
    if len(data) != KEY_BYTES:
        raise KeyProtectionError("Entschluesselter Schluessel hat eine unerwartete Laenge")
    return data


def load_or_create_key(path: Path) -> bytes:
    """Liest den Schluessel aus key.bin oder erzeugt ihn neu (atomar geschrieben)."""
    if path.exists():
        blob = path.read_bytes()
        if not blob:
            raise KeyProtectionError("key.bin ist leer")
        return unprotect_key(blob)
    key = secrets.token_bytes(KEY_BYTES)
    blob = protect_key(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".bin.tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)
    log.info("Neuer Datenbankschluessel erzeugt und per DPAPI geschuetzt")
    return key


def key_hex(key: bytes) -> str:
    if len(key) != KEY_BYTES:
        raise ValueError("Schluessel muss 32 Byte lang sein")
    return key.hex()


# --------------------------------------------------------------------------- SQLCipher

def _db_uri(path: Path, read_only: bool) -> str:
    uri = path.resolve().as_uri()
    return f"{uri}?mode=ro" if read_only else uri


def open_database(path: Path, key: bytes, *, read_only: bool = False, create: bool = True) -> sqlcipher3.Connection:
    """Oeffnet die verschluesselte Datenbank und setzt alle sicherheitsrelevanten PRAGMAs.

    Reihenfolge ist wichtig: cipher_log_level (kein stderr-Rauschen bei falschem Schluessel) ->
    key (Raw-Key, kein PBKDF2) -> temp_store=MEMORY (keine Klartext-Sortierdateien in %TEMP%).
    Der Writer schaltet ausserdem WAL und inkrementelles auto_vacuum ein (letzteres wirkt nur
    vor dem Anlegen der ersten Tabelle, ist auf bestehenden Datenbanken unschaedlich).
    Die Verbindung laeuft im Autocommit-Modus; Transaktionen werden explizit mit BEGIN/COMMIT gefuehrt.
    """
    path = Path(path)
    if not create and not path.exists():
        raise FileNotFoundError(path)
    if read_only:
        con = sqlcipher3.connect(_db_uri(path, True), uri=True, check_same_thread=False, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlcipher3.connect(str(path), check_same_thread=False, isolation_level=None)
    try:
        try:
            con.execute("PRAGMA cipher_log_level = NONE")
        except sqlcipher3.DatabaseError:  # pragma: no cover - aeltere SQLCipher-Version
            pass
        con.execute(f"PRAGMA key = \"x'{key_hex(key)}'\"")
        con.execute("PRAGMA temp_store = MEMORY")
        con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # Schluesselpruefung VOR allen PRAGMAs, die den Dateikopf lesen (auto_vacuum, journal_mode ...)
        try:
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlcipher3.DatabaseError as e:
            raise WrongKeyError(f"Datenbank {path.name} ist mit diesem Schluessel nicht lesbar: {e}") from e
        if read_only:
            con.execute("PRAGMA query_only = 1")
        else:
            con.execute("PRAGMA auto_vacuum = INCREMENTAL")
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
            con.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")
            # secure_delete=ON nullt freigegebene Seiten. Wichtig fuer ein Privacy-Tool: nach "Eintrag loeschen"
            # bzw. der Retention duerfen die (verschluesselten) Bild-/Text-Seiten nicht als Freelist-Rest im
            # DB-File carve-bar bleiben. Kostet etwas I/O beim Loeschen, was hier vertretbar ist.
            con.execute("PRAGMA secure_delete = ON")
        con.execute("PRAGMA foreign_keys = ON")
    except Exception:
        con.close()
        raise
    con.row_factory = sqlcipher3.Row
    return con


def is_encrypted_file(path: Path) -> bool:
    """True, wenn die Datei nicht mit dem Klartext-SQLite-Header beginnt (grober Sanity-Check)."""
    try:
        with open(path, "rb") as f:
            return not f.read(16).startswith(b"SQLite format 3")
    except FileNotFoundError:
        return False
