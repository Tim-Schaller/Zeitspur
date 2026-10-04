"""Zugangsdaten der Plugins (Tokens, geheime Kalender-Links): DPAPI-geschuetzt, je Plugin eine Datei.

Liegt im Datenordner unter plugins\\<plugin-id>.bin, nie in config.yaml. Die Zusatzentropie enthaelt die
Plugin-Id - eine Datei laesst sich so nicht einem anderen Plugin unterschieben. Teams und Dawarich haben
eigene Dateien aus der Zeit vor diesem Speicher und bleiben dabei.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .config import data_dir

log = logging.getLogger(__name__)

ENTROPY = b"Zeitspur-Plugin-credentials-v1"   # darf sich nie aendern, sonst sind gespeicherte Zugangsdaten unlesbar
_ID = re.compile(r"[a-z][a-z0-9_]{1,40}")


def _entropy(plugin_id: str) -> bytes:
    return ENTROPY + b"\n" + plugin_id.encode("ascii")


def path(plugin_id: str) -> Path:
    if not _ID.fullmatch(plugin_id):
        raise ValueError(f"Ungueltige Plugin-Id: {plugin_id!r}")
    return data_dir() / "plugins" / f"{plugin_id}.bin"


def save(plugin_id: str, values: dict[str, str]) -> Path:
    from .crypto import dpapi_protect

    payload = json.dumps({str(k): str(v) for k, v in values.items()}, ensure_ascii=False).encode("utf-8")
    p = path(plugin_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".bin.tmp")
    tmp.write_bytes(dpapi_protect(payload, _entropy(plugin_id), f"Zeitspur {plugin_id} credentials"))
    os.replace(tmp, p)
    return p


def load(plugin_id: str) -> dict[str, str] | None:
    """Die gespeicherten Werte - oder None, wenn nichts (Lesbares) da ist."""
    from .crypto import KeyProtectionError, dpapi_unprotect

    p = path(plugin_id)
    if not p.exists():
        return None
    try:
        data = json.loads(dpapi_unprotect(p.read_bytes(), _entropy(plugin_id)).decode("utf-8"))
    except (KeyProtectionError, ValueError) as e:
        log.warning("Zugangsdaten von %s nicht lesbar: %s", plugin_id, e)
        return None
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None


def exists(plugin_id: str) -> bool:
    return path(plugin_id).exists()


def clear(plugin_id: str) -> bool:
    p = path(plugin_id)
    if p.exists():
        p.unlink()
        return True
    return False
