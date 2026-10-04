"""UI-Smoke-Test: startet den Dienst mit separatem Datenordner, wartet auf das Fenster, macht einen
Screenshot des Fensters (nur dessen Rechteck), beendet die Instanz ueber das Quit-Event und prueft das Log.

  .venv\\Scripts\\python tools\\ui_smoke.py --out <ordner> [--case firstrun|demo|all]
"""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

import win32gui  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from zeitspur import winutil  # noqa: E402

TITLE = "Zeitspur"
KEEP_DEMO = False


def find_window(timeout: float = 45.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found: list[int] = []

        def _cb(h, _):
            if win32gui.IsWindowVisible(h) and TITLE in win32gui.GetWindowText(h):
                found.append(h)
            return True

        win32gui.EnumWindows(_cb, None)
        if found:
            return found[0]
        time.sleep(0.5)
    return None


def run_case(name: str, out_dir: Path, seed_days: int, settle: float, exe: str | None = None) -> bool:
    data_dir = Path(tempfile.gettempdir()) / f"ZeitspurSmoke-{name}"
    if not (KEEP_DEMO and name == "demo" and (data_dir / "zeitspur.db").exists()):
        shutil.rmtree(data_dir, ignore_errors=True)
    else:
        seed_days = 0
    env = dict(os.environ, ZEITSPUR_DATA_DIR=str(data_dir))
    if seed_days:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "seed_demo_db.py"), "--days", str(seed_days)],
                           env=env, cwd=ROOT, capture_output=True, text=True)
        print(f"[{name}] seed: rc={r.returncode} {r.stdout.strip()[-200:]} {r.stderr.strip()[-300:]}")
    cmd = [exe, "--show"] if exe else [sys.executable, "-m", "zeitspur.service_main", "--show"]
    proc = subprocess.Popen(cmd, env=env, cwd=ROOT)
    ok = True
    hwnd = find_window()
    if hwnd is None:
        print(f"[{name}] FEHLER: Fenster nicht gefunden")
        ok = False
    else:
        time.sleep(settle)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.5)
        rect = win32gui.GetWindowRect(hwnd)
        img = ImageGrab.grab(bbox=rect, all_screens=True)
        out = out_dir / f"{name}.png"
        img.save(out)
        print(f"[{name}] Screenshot {out} {img.size} rect={rect}")
    signalled = winutil.signal_event(winutil.EVENT_QUIT)
    print(f"[{name}] Quit-Signal gesendet: {signalled}")
    try:
        code = proc.wait(timeout=20)
        print(f"[{name}] Prozess beendet, exit={code}")
        ok = ok and code == 0
    except subprocess.TimeoutExpired:
        print(f"[{name}] FEHLER: Prozess haengt, wird abgeschossen")
        proc.kill()
        ok = False
    log = data_dir / "logs" / "service.log"
    if log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        errors = [l for l in lines if " ERROR " in l or "Traceback" in l or " WARNING " in l]
        print(f"[{name}] Log: {len(lines)} Zeilen, {len(errors)} Warnungen/Fehler")
        for l in lines[-12:]:
            print("   ", l)
        for l in errors[:10]:
            print("  !!", l)
        if any("Traceback" in l or " ERROR " in l for l in lines):
            ok = False
    else:
        print(f"[{name}] FEHLER: kein Log unter {log}")
        ok = False
    leftovers = sorted(p.name for p in data_dir.iterdir()) if data_dir.exists() else []
    print(f"[{name}] Datenordner: {leftovers}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--case", default="all", choices=["firstrun", "demo", "all"])
    p.add_argument("--settle", type=float, default=6.0)
    p.add_argument("--exe", help="gepackte Zeitspur.exe statt python -m testen")
    p.add_argument("--keep-demo", action="store_true", help="Demo-Datenordner nicht neu erzeugen (fuer mcp_smoke)")
    args = p.parse_args()
    global KEEP_DEMO
    KEEP_DEMO = args.keep_demo
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    if args.case in ("firstrun", "all"):
        results.append(run_case("firstrun", out, seed_days=0, settle=args.settle, exe=args.exe))
    if args.case in ("demo", "all"):
        results.append(run_case("demo", out, seed_days=2, settle=args.settle + 3, exe=args.exe))
    print("ERGEBNIS:", "OK" if all(results) else "FEHLER")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
