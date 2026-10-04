"""Entfernt aus installer/tesseract-portable alle DLLs, die tesseract.exe nicht (transitiv) importiert.

Der UB-Mannheim-Installer enthaelt Bibliotheken der Trainingstools (Pango, Cairo, GLib, ICU ...),
die fuer die reine Texterkennung nicht gebraucht werden. Die Import-Tabellen werden mit `pefile`
rekursiv ausgewertet; anschliessend wird ein Smoke-Test (--version, --list-langs) ausgefuehrt.

Aufruf: .venv\\Scripts\\python tools\\trim_tesseract.py [--dry-run]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pefile

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "installer" / "tesseract-portable"
CREATE_NO_WINDOW = 0x08000000


def imported_dlls(pe_path: Path) -> set[str]:
    pe = pefile.PE(str(pe_path), fast_load=True)
    pe.parse_data_directories(directories=[
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
    ])
    names: set[str] = set()
    for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
        for entry in getattr(pe, attr, []) or []:
            if entry.dll:
                names.add(entry.dll.decode("ascii", "ignore").lower())
    pe.close()
    return names


def main() -> int:
    dry = "--dry-run" in sys.argv
    exe = PORTABLE / "tesseract.exe"
    if not exe.exists():
        print(f"nicht gefunden: {exe}", file=sys.stderr)
        return 1
    available = {p.name.lower(): p for p in PORTABLE.glob("*.dll")}
    needed: set[str] = set()
    queue = [exe]
    while queue:
        current = queue.pop()
        for dll in imported_dlls(current):
            if dll in available and dll not in needed:
                needed.add(dll)
                queue.append(available[dll])
    unused = sorted(set(available) - needed)
    total = sum(available[n].stat().st_size for n in unused)
    print(f"benoetigt: {len(needed)} DLLs, entfernbar: {len(unused)} DLLs ({total / 1e6:.1f} MB)")
    for n in unused:
        print("  -", n)
        if not dry:
            available[n].unlink()
    if dry:
        return 0
    # Smoke-Test nach dem Trimmen
    for args in (["--version"], ["--list-langs", "--tessdata-dir", str(PORTABLE / "tessdata")]):
        r = subprocess.run([str(exe), *args], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        out = (r.stdout + r.stderr).strip().splitlines()
        print(f"tesseract {' '.join(args)} -> rc={r.returncode}: {out[:1]}")
        if r.returncode != 0:
            print("Smoke-Test fehlgeschlagen - Bundle bitte mit tools/fetch_tesseract.ps1 neu erzeugen", file=sys.stderr)
            return 2
    size = sum(p.stat().st_size for p in PORTABLE.rglob("*") if p.is_file())
    print(f"Bundle jetzt {size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
