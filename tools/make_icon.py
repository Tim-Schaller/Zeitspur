"""Erzeugt assets/zeitspur.ico (Multi-Size) aus dem per Pillow gezeichneten Tray-Symbol."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zeitspur.tray import save_ico  # noqa: E402

if __name__ == "__main__":
    out = save_ico(ROOT / "assets" / "zeitspur.ico")
    print(f"Icon geschrieben: {out} ({out.stat().st_size} Bytes)")
