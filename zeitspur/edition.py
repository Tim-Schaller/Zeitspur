"""Welche Funktionen in dieser Ausgabe von Zeitspur enthalten sind.

Die Entwicklerausgabe (Quelltext, normaler Build) enthaelt alles. Der Release-Build (build.ps1 -Release)
packt Funktionen, die noch nicht fertig sind, gar nicht erst ein - derzeit die Standort-Historie (Dawarich)
samt Karte und bekannten Orten. Massgeblich ist allein, ob das Modul im Paket steckt: Einen Schalter, den
man beim Bauen vergessen koennte, gibt es nicht.

Ebenso bei Updates: Nur der Release-Build bringt die Datei release_channel.txt mit (zeitspur.spec legt sie
an) und aktualisiert sich selbst. Der eigene Build und der Quelltextbetrieb nicht - ein Release ersetzte dort
Funktionen, die er nicht enthaelt.
"""
from __future__ import annotations

import os
from importlib.util import find_spec
from pathlib import Path

# Standort-Historie (Plugin "dawarich"), Karte und bekannte Orte
LOCATIONS: bool = find_spec("zeitspur.dawarich") is not None


def _update_channel() -> str | None:
    try:
        return Path(__file__).with_name("release_channel.txt").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# Update-Kanal des Release-Builds ("release"); None = keine automatischen Updates. Fuer Tests laesst sich ein
# eigener Kanal ueber ZEITSPUR_UPDATE_URL setzen (siehe updater.py) - die Signaturpruefung gilt auch dann.
UPDATE_CHANNEL: str | None = _update_channel() or ("test" if os.environ.get("ZEITSPUR_UPDATE_URL") else None)


def features() -> dict[str, bool]:
    """Fuer die Oberflaeche: was diese Ausgabe kann."""
    return {"locations": LOCATIONS, "updates": UPDATE_CHANNEL is not None}
