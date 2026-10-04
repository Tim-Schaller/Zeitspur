"""Release-Schluessel fuer signierte Updates (siehe zeitspur/updater.py).

Der private Schluessel (Ed25519) liegt DPAPI-geschuetzt unter %USERPROFILE%\\.zeitspur\\ - lesbar nur fuer
dieses Windows-Konto. Bewusst nicht unter AppData: Aus App-Containern heraus (etwa der Claude-App) leitet
Windows Schreibzugriffe dorthin in eine private Kopie um, die anderen Programmen verborgen bliebe.
Zeitspur selbst kennt nur den oeffentlichen Teil (updater.PUBLIC_KEY).

  python tools/release_key.py init                 einmalig: Schluessel erzeugen, oeffentlichen Teil ausgeben
  python tools/release_key.py public               oeffentlichen Teil ausgeben
  python tools/release_key.py manifest --setup S --version V --notes-file N [--url U] --out latest.json
  python tools/release_key.py verify --manifest latest.json [--setup S]
  python tools/release_key.py backup --out sicherung.pem    passwortgeschuetzte Sicherung (Passwort-Abfrage)
  python tools/release_key.py verify-backup --in sicherung.pem   Sicherung pruefen (Passwort und Schluessel)
  python tools/release_key.py restore --in sicherung.pem    Sicherung auf einem neuen PC einspielen

Geht der Schluessel verloren, gibt es keine automatischen Updates mehr fuer bereits installierte Versionen -
sie muessen einmal von Hand auf eine Version mit neuem Schluessel aktualisiert werden. Also: Sicherung anlegen
und zusammen mit dem Passwort im Passwortmanager aufbewahren.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zeitspur import updater  # noqa: E402

KEY_PATH = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".zeitspur" / "release-signing-key.dpapi"
ENTROPY = b"Zeitspur-Release-Key-v1"   # darf sich nie aendern, sonst ist der Schluessel unlesbar
DESCRIPTION = "Zeitspur release signing key"


def _protect(raw: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptProtectData(raw, DESCRIPTION, ENTROPY, None, None, 0x01)


def _unprotect(blob: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptUnprotectData(blob, ENTROPY, None, None, 0x01)[1]


def _check_not_redirected(path: Path) -> None:
    from zeitspur import winutil

    shadow = winutil.container_shadow(path.parent, probe=True)
    if shadow:
        sys.exit(f"Abbruch: Schreibzugriffe auf {path.parent} werden in einen App-Container umgeleitet ({shadow}). "
                 "Bitte aus einer normalen PowerShell ausfuehren.")


def _store(private_key) -> None:
    from cryptography.hazmat.primitives import serialization

    raw = private_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _check_not_redirected(KEY_PATH)
    tmp = KEY_PATH.with_suffix(".tmp")
    tmp.write_bytes(_protect(raw))
    os.replace(tmp, KEY_PATH)


def load_private_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not KEY_PATH.is_file():
        sys.exit(f"Kein Release-Schluessel unter {KEY_PATH} - zuerst 'init' (oder 'restore') ausfuehren.")
    return Ed25519PrivateKey.from_private_bytes(_unprotect(KEY_PATH.read_bytes()))


def public_b64(private_key) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def build_manifest(setup: Path, version: str, notes: str, url: str | None, private_key) -> dict:
    data = setup.read_bytes()
    payload = {"version": version, "file": setup.name, "sha256": hashlib.sha256(data).hexdigest(),
               "size": len(data), "notes": notes.strip(), "published": date.today().isoformat()}
    if url:
        payload["url"] = url
    updater._check_payload(payload)   # dieselben Regeln wie beim Empfaenger
    return {"payload": payload, "signature": updater.sign_payload(payload, private_key)}


def cmd_init(_args) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if KEY_PATH.exists():
        sys.exit(f"Es gibt schon einen Release-Schluessel ({KEY_PATH}) - er wird nicht ueberschrieben.")
    key = Ed25519PrivateKey.generate()
    _store(key)
    print(f"Release-Schluessel angelegt: {KEY_PATH}")
    print(f"Oeffentlicher Teil (in zeitspur/updater.py als PUBLIC_KEY eintragen): {public_b64(key)}")
    print("Jetzt eine Sicherung anlegen: python tools/release_key.py backup --out <Datei>")


def cmd_public(_args) -> None:
    print(public_b64(load_private_key()))


def cmd_manifest(args) -> None:
    key = load_private_key()
    if public_b64(key) != updater.PUBLIC_KEY:
        sys.exit("Abbruch: Der Schluessel passt nicht zu updater.PUBLIC_KEY - Clients koennten nichts pruefen.")
    notes = Path(args.notes_file).read_text(encoding="utf-8") if args.notes_file else ""
    doc = build_manifest(Path(args.setup), args.version, notes, args.url, key)
    Path(args.out).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Manifest geschrieben: {args.out} (Version {args.version}, SHA-256 {doc['payload']['sha256']})")


def cmd_verify(args) -> None:
    """Prueft wie ein Client: Signatur mit dem eingebauten oeffentlichen Schluessel, optional das Setup."""
    payload = updater.verify_manifest(Path(args.manifest).read_bytes())
    if args.setup and not updater.file_matches(Path(args.setup), payload["sha256"], payload["size"]):
        sys.exit("Setup passt nicht zum Manifest (Groesse oder SHA-256)")
    print(f"Manifest gueltig: Version {payload['version']}, {payload['file']}, {payload['size']} Bytes")


def cmd_backup(args) -> None:
    from cryptography.hazmat.primitives import serialization

    key = load_private_key()
    out = Path(args.out)
    if out.exists():
        sys.exit(f"{out} gibt es schon - bitte einen neuen Dateinamen waehlen.")
    password = getpass.getpass("Passwort fuer die Sicherung: ")
    if len(password) < 12 or password != getpass.getpass("Passwort wiederholen: "):
        sys.exit("Abbruch: Passwoerter verschieden oder kuerzer als 12 Zeichen.")
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(password.encode("utf-8")))
    out.write_bytes(pem)
    print(f"Sicherung geschrieben: {out} - zusammen mit dem Passwort sicher aufbewahren, nicht ins Repo legen.")


def cmd_verify_backup(args) -> None:
    """Prueft eine Sicherung, ohne etwas zu veraendern: Oeffnet das Passwort sie, und steckt darin genau der
    Schluessel, mit dem Zeitspur Updates prueft? Eine nie getestete Sicherung taugt im Ernstfall oft nichts."""
    from cryptography.hazmat.primitives import serialization

    password = getpass.getpass("Passwort der Sicherung: ")
    try:
        key = serialization.load_pem_private_key(Path(args.input).read_bytes(), password.encode("utf-8"))
    except (ValueError, TypeError):
        sys.exit("Die Sicherung laesst sich mit diesem Passwort NICHT oeffnen.")
    if public_b64(key) != updater.PUBLIC_KEY:
        sys.exit("Die Sicherung enthaelt einen ANDEREN Schluessel als den, mit dem Zeitspur Updates prueft.")
    print("Sicherung in Ordnung: Das Passwort oeffnet sie, und darin steckt der Release-Schluessel von Zeitspur.")


def cmd_restore(args) -> None:
    from cryptography.hazmat.primitives import serialization

    if KEY_PATH.exists():
        sys.exit(f"Es gibt schon einen Release-Schluessel ({KEY_PATH}).")
    password = getpass.getpass("Passwort der Sicherung: ")
    key = serialization.load_pem_private_key(Path(args.input).read_bytes(), password.encode("utf-8"))
    _store(key)
    print(f"Release-Schluessel eingespielt: {KEY_PATH} (oeffentlicher Teil {public_b64(key)})")


def main() -> None:
    p = argparse.ArgumentParser(description="Release-Schluessel fuer signierte Zeitspur-Updates")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("public").set_defaults(func=cmd_public)
    m = sub.add_parser("manifest")
    m.add_argument("--setup", required=True)
    m.add_argument("--version", required=True)
    m.add_argument("--notes-file")
    m.add_argument("--url")
    m.add_argument("--out", required=True)
    m.set_defaults(func=cmd_manifest)
    v = sub.add_parser("verify")
    v.add_argument("--manifest", required=True)
    v.add_argument("--setup")
    v.set_defaults(func=cmd_verify)
    b = sub.add_parser("backup")
    b.add_argument("--out", required=True)
    b.set_defaults(func=cmd_backup)
    r = sub.add_parser("restore")
    r.add_argument("--in", dest="input", required=True)
    r.set_defaults(func=cmd_restore)
    vb = sub.add_parser("verify-backup")
    vb.add_argument("--in", dest="input", required=True)
    vb.set_defaults(func=cmd_verify_backup)
    args = p.parse_args()
    try:
        args.func(args)
    except updater.UpdateError as e:
        sys.exit(f"Abbruch: {e}")


if __name__ == "__main__":
    main()
