"""Werte, die sich nie aendern duerfen: die DPAPI-Zusatzentropie (sonst sind Schluessel und Zugangsdaten unlesbar)
und das Signaturverfahren der Updates (sonst lehnen installierte Versionen jedes Update ab).

Geprueft ueber Pruefsummen und eine feste Testsignatur, damit ein versehentliches Suchen-und-Ersetzen auffaellt."""
import base64
import hashlib
import importlib.util
from pathlib import Path

from zeitspur import credentials, crypto, dawarich, teams, updater


def _release_key_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "release_key.py"
    spec = importlib.util.spec_from_file_location("release_key_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:16]


def test_dpapi_entropy_never_changes():
    assert {
        "db_key": _fingerprint(crypto.ENTROPY),
        "teams": _fingerprint(teams.TEAMS_ENTROPY),
        "dawarich": _fingerprint(dawarich.DAWARICH_ENTROPY),
        "release_key": _fingerprint(_release_key_tool().ENTROPY),
        "plugins": _fingerprint(credentials.ENTROPY),
    } == {
        "db_key": "c3360f836d660cc5",
        "teams": "aa03b326c9a5d945",
        "dawarich": "34092fa70fbdcf89",
        "release_key": "6178f5ed6bd3830b",
        "plugins": "8f96a19f0087785f",
    }


def test_update_signature_protocol_never_changes():
    """Ed25519 ist deterministisch: gleicher Schluessel und gleicher Inhalt ergeben immer dieselbe Signatur."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    assert _fingerprint(updater.SIGNATURE_CONTEXT) == "b90ec44c307fe1c7"
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    payload = {"version": "1.2.3", "file": "ZeitspurSetup-1.2.3.exe", "sha256": "0" * 64, "size": 1, "notes": "Ä"}
    assert updater.sign_payload(payload, key) == (
        "qealAFwU2DG8Sd0sn/6XEnSCM7WXE3PJYMizGY7p0fo0QID0FOOB3jvI3STC9e0UPxdmmU+zVxQg0cGHDU/pAg==")
    assert len(base64.b64decode(updater.PUBLIC_KEY)) == 32
