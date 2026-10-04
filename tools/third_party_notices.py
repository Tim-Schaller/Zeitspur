"""Erzeugt THIRD-PARTY-NOTICES.txt: Lizenzen aller Komponenten, die mit Zeitspur ausgeliefert werden.

Die Python-Pakete stammen aus requirements.txt samt allen Abhaengigkeiten, so wie sie in der venv installiert
sind - Name, Version, Lizenz und der vollstaendige Text ihrer Lizenzdateien. Dazu kommen die Bestandteile, die
nicht aus pip stammen (Python selbst, Tesseract, Leaflet, WebView2-Bootstrapper, PyInstaller-Bootloader).
build.ps1 ruft das nach jedem PyInstaller-Lauf auf; die Datei landet im Programmordner.

  python tools/third_party_notices.py --out dist\\Zeitspur\\THIRD-PARTY-NOTICES.txt [--release]
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import sys
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.IGNORECASE)


def runtime_distributions() -> list[md.Distribution]:
    """requirements.txt plus alle (nicht optionalen) Abhaengigkeiten, alphabetisch."""
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    todo = [Requirement(l.strip()).name for l in lines if l.strip() and not l.lstrip().startswith("#")]
    found: dict[str, md.Distribution] = {}
    while todo:
        name = todo.pop()
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in found:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        found[key] = dist
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                todo.append(req.name)
    return [found[k] for k in sorted(found)]


def license_name(dist: md.Distribution) -> str:
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    text = (meta.get("License") or "").strip()
    return text.splitlines()[0][:80] if text else "siehe Lizenztext"


def license_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    texts = []
    for f in dist.files or []:
        if LICENSE_FILE.search(str(f).replace("\\", "/")):
            try:
                texts.append((str(f), Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace").strip()))
            except OSError:
                continue
    return texts


def _section(title: str, body: str) -> str:
    return f"{'=' * 100}\n{title}\n{'=' * 100}\n\n{body.strip()}\n\n"


def build(release: bool) -> str:
    parts = ["Zeitspur - Lizenzen der mitgelieferten Komponenten\n\n"
             "Zeitspur selbst steht unter der MIT-Lizenz mit Commons-Clause-Zusatz (LICENSE.txt). Die folgenden\n"
             "Bestandteile behalten ihre eigenen Lizenzen. pystray (LGPL-3.0) liegt unveraendert als eigene Dateien\n"
             "unter _internal\\pystray und laesst sich dort durch eine andere Version ersetzen.\n\n"]
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    parts.append(_section(f"Python {sys.version.split()[0]} - PSF License Version 2",
                          python_license.read_text(encoding="utf-8", errors="replace")
                          if python_license.exists() else "https://docs.python.org/3/license.html"))
    tesseract = ROOT / "installer" / "tesseract-portable" / "doc" / "LICENSE"
    parts.append(_section("Tesseract OCR und Sprachdaten tessdata_fast - Apache License 2.0",
                          "Ordner tesseract\\ im Programmverzeichnis. https://github.com/tesseract-ocr/tesseract\n\n"
                          + (tesseract.read_text(encoding="utf-8", errors="replace") if tesseract.exists()
                             else "https://www.apache.org/licenses/LICENSE-2.0")))
    if not release:
        leaflet = ROOT / "zeitspur" / "timeline_ui" / "vendor" / "LICENSE-leaflet.txt"
        parts.append(_section("Leaflet 1.9.4 - BSD 2-Clause", leaflet.read_text(encoding="utf-8")))
    parts.append(_section("Microsoft Edge WebView2 Evergreen Bootstrapper",
                          "Nur im Setup enthalten und nur ausgefuehrt, wenn die WebView2-Runtime fehlt. Unveraendert "
                          "weitergegeben gemaess Microsofts Verteilungsbedingungen fuer WebView2:\n"
                          "https://developer.microsoft.com/microsoft-edge/webview2/"))
    parts.append(_section("PyInstaller-Bootloader - GPL 2.0 mit Bootloader-Ausnahme",
                          "Zeitspur.exe enthaelt den Bootloader von PyInstaller. Dessen Ausnahme erlaubt die "
                          "Weitergabe des erzeugten Programms unter beliebiger Lizenz. https://pyinstaller.org/en/stable/license.html"))
    for dist in runtime_distributions():
        name, version = dist.metadata["Name"], dist.version
        home = dist.metadata.get("Home-page") or next(
            (u.split(",", 1)[-1].strip() for u in dist.metadata.get_all("Project-URL") or []), "")
        body = [f"Lizenz: {license_name(dist)}", f"Projekt: {home}" if home else ""]
        for path, text in license_texts(dist):
            body.append(f"--- {path}\n\n{text}")
        if len(body) == 2:
            body.append("(Das Paket bringt keine Lizenzdatei mit; massgeblich ist die oben genannte Lizenz.)")
        parts.append(_section(f"{name} {version}", "\n\n".join(b for b in body if b)))
    return "".join(parts)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", required=True)
    p.add_argument("--release", action="store_true", help="Release-Ausgabe (ohne Leaflet)")
    args = p.parse_args()
    text = build(args.release)
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"{args.out}: {text.count('=' * 100) // 2} Abschnitte, {len(text) // 1024} KB")


if __name__ == "__main__":
    main()
