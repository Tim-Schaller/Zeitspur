"""Screenshots der Zeitspur-Oberflaeche fuer README und Projektseite - mit erfundenen Demo-Daten.

Gerendert wird die echte Oberflaeche (dieselben HTML-, CSS- und JS-Dateien wie im Programmfenster, mit den
Antworten der echten Bridge ueber einer Demo-Datenbank, siehe tools/demo_ui.py) von Edge im Headless-Modus -
dieselbe Chromium-Engine wie WebView2 im Fenster. Das klappt ohne Bildschirm (auch bei gesperrtem PC), oeffnet kein
Fenster und benutzt ein eigenes Wegwerf-Profil statt des Edge-Profils. Dazu das Programmsymbol als PNG.

  .venv\\Scripts\\python tools\\screenshots.py              -> docs\\images\\*.png
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

OUT = ROOT / "docs" / "images"
EDGE = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
SIZE = (1440, 900)
UPDATE_BAR = (0x1B, 0x2A, 0x44)   # Hintergrund des Update-Hinweises im dunklen Design (style.css)


def below_update_bar(img: Image.Image) -> int:
    """Ausschnitt bis knapp unter die Tageszeile nach dem Update-Hinweis - waechst mit der Laenge des Changelogs."""
    rows = [y for y in range(img.height) if img.getpixel((4, y)) == UPDATE_BAR]
    return rows[-1] + 40 if rows else img.height


# Ansicht -> (Dateiname, Fenstergroesse, Hoehe des Ausschnitts von oben: Zahl, Funktion oder None = ganzes Fenster)
SHOTS = {"": ("zeitstrahl", SIZE, 488), "detail": ("details", (1440, 1080), None),
         "plugins": ("plugins", (1440, 1080), None), "setup": ("ersteinrichtung", (880, 900), None),
         "update": ("update", SIZE, below_update_bar)}


def save_logo(out: Path = OUT / "logo.png", size: int = 512) -> Path:
    from zeitspur.tray import make_icon_image
    out.parent.mkdir(parents=True, exist_ok=True)
    make_icon_image((52, 168, 83), size).save(out, optimize=True)
    return out


def edge() -> str:
    found = next((p for p in EDGE if Path(p).is_file()), None) or shutil.which("msedge")
    if not found:
        raise SystemExit("Microsoft Edge nicht gefunden")
    return found


def render(page: Path, out: Path, profile: Path, *, size: tuple[int, int] = SIZE,
           height: int | Callable[[Image.Image], int] | None = None) -> Path:
    # --force-dark-mode: dunkles Design unabhaengig von der Windows-Einstellung, damit die Bilder gleich bleiben
    subprocess.run([edge(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                    "--disable-extensions", "--force-dark-mode", f"--user-data-dir={profile}",
                    f"--window-size={size[0]},{size[1]}", "--force-device-scale-factor=1",
                    "--virtual-time-budget=6000", f"--screenshot={out}", page.as_uri()],
                   check=True, capture_output=True, timeout=120)
    img = Image.open(out).convert("RGB")
    if callable(height):
        height = height(img)
    if height:
        img = img.crop((0, 0, img.width, min(img.height, height)))
    img.save(out, optimize=True)
    return out


def main() -> int:
    from tools.demo_ui import write_previews

    OUT.mkdir(parents=True, exist_ok=True)
    print("Logo:", save_logo().relative_to(ROOT))
    with tempfile.TemporaryDirectory(prefix="zeitspur-shots-") as tmp:
        pages = write_previews(Path(tmp) / "seiten", modes=tuple(SHOTS))
        for mode, (name, size, height) in SHOTS.items():
            out = render(pages[mode], OUT / f"{name}.png", Path(tmp) / "edge-profil", size=size, height=height)
            print(f"  {out.relative_to(ROOT)} {Image.open(out).size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
