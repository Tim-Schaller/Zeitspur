"""Misst CPU-Last und Aufnahmeverhalten des Dienstes waehrend eines kurzen Echtbetriebs.

Startet den Dienst mit einem separaten Datenordner (ZEITSPUR_DATA_DIR) und einer Konfiguration, die die
Leerlauf-Pause aussetzt, protokolliert sekuendlich die CPU-Last von Zeitspur (inkl. tesseract-Kinder)
und gibt am Ende Statistik aus. Der Datenordner wird anschliessend geloescht (--keep behaelt ihn).

  .venv\\Scripts\\python tools\\perf_probe.py --seconds 120 [--exe dist\\Zeitspur\\Zeitspur.exe]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zeitspur import winutil  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=int, default=120)
    p.add_argument("--exe")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--interval", type=int, default=5)
    args = p.parse_args()

    data_dir = Path(tempfile.gettempdir()) / "ZeitspurPerf"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)
    cfg = {"capture_interval_seconds": args.interval, "idle_pause_minutes": 240, "retention_days": 14}
    (data_dir / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    env = dict(os.environ, ZEITSPUR_DATA_DIR=str(data_dir))
    cmd = [args.exe] if args.exe else [sys.executable, "-m", "zeitspur.service_main"]
    proc = subprocess.Popen(cmd, env=env, cwd=ROOT)
    ps = psutil.Process(proc.pid)
    cores = psutil.cpu_count(logical=True) or 1
    samples: list[tuple[float, float, float]] = []  # (Dienst %, Tesseract %, WebView2 %) je in "% eines Kerns"
    time.sleep(8)  # WebView2 des versteckten Fensters initialisiert sich in den ersten Sekunden
    ps.cpu_percent(None)
    # Tesseract-Kinder leben nur ~1 s: kumulierte CPU-Zeit je PID feinmaschig (alle 0,2 s) abgreifen;
    # WebView2-Prozesse (msedgewebview2.exe) getrennt ausweisen
    child_cpu: dict[int, float] = {}
    child_name: dict[int, str] = {}
    deadline = time.time() + args.seconds
    prev = {"tess": 0.0, "web": 0.0}
    try:
        while time.time() < deadline and proc.poll() is None:
            t_end = time.time() + 1.0
            while time.time() < t_end:
                for ch in ps.children(recursive=True):
                    try:
                        ct = ch.cpu_times()
                        if ch.pid not in child_name:
                            child_name[ch.pid] = ch.name().lower()
                        child_cpu[ch.pid] = ct.user + ct.system
                    except psutil.Error:
                        pass
                time.sleep(0.2)
            svc = ps.cpu_percent(None)
            tess_total = sum(v for pid, v in child_cpu.items() if "tesseract" in child_name.get(pid, ""))
            web_total = sum(v for pid, v in child_cpu.items() if "tesseract" not in child_name.get(pid, ""))
            samples.append((svc, (tess_total - prev["tess"]) * 100.0, (web_total - prev["web"]) * 100.0))
            prev = {"tess": tess_total, "web": web_total}
        mem = ps.memory_info().rss / 1e6
    finally:
        winutil.signal_event(winutil.EVENT_QUIT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not samples:
        print("keine Messwerte")
        return 1
    svc = [a for a, _, _ in samples]
    tess = [b for _, b, _ in samples]
    web = [c for _, _, c in samples]
    total = [a + b + c for a, b, c in samples]
    avg = lambda xs: sum(xs) / len(xs)  # noqa: E731
    tess_runs = sum(1 for n in child_name.values() if "tesseract" in n)
    tess_cpu = sum(v for pid, v in child_cpu.items() if "tesseract" in child_name.get(pid, ""))
    web_procs = sorted({n for n in child_name.values() if "tesseract" not in n})
    print(f"Messdauer {len(samples)} s, {cores} logische Kerne, RSS Dienst {mem:.0f} MB")
    print(f"Dienst    : avg {avg(svc):.1f} % eines Kerns (= {avg(svc) / cores:.2f} % gesamt), max {max(svc):.0f} %")
    print(f"Tesseract : avg {avg(tess):.1f} % eines Kerns, max {max(tess):.0f} % ({tess_runs} Aufrufe, "
          f"{tess_cpu:.1f} CPU-Sekunden, {tess_cpu / max(1, tess_runs):.2f} s je Aufruf)")
    print(f"WebView2  : avg {avg(web):.1f} % eines Kerns, max {max(web):.0f} % (Prozesse: {', '.join(web_procs) or '-'})")
    print(f"Gesamt    : avg {avg(total):.1f} % eines Kerns (= {avg(total) / cores:.2f} % gesamt), max {max(total):.0f} %")
    # Datenbankstatistik
    stat = subprocess.run([sys.executable, str(ROOT / "tools" / "dbstat.py"), "--days", "1"], env=env, cwd=ROOT,
                          capture_output=True, text=True)
    print(stat.stdout.strip()[:800])
    log = data_dir / "logs" / "service.log"
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        bad = [l for l in lines if " ERROR " in l or "Traceback" in l]
        print(f"Log: {len(lines)} Zeilen, {len(bad)} Fehler")
        for l in bad[:5]:
            print("  !!", l[:200])
    leftovers = sorted(p.name for p in data_dir.iterdir())
    print("Datenordner:", leftovers)
    temp_leaks = [f for f in os.listdir(tempfile.gettempdir()) if f.startswith(("tess_", "etilqs_"))]
    print("Temp-Dateien tess_*/etilqs_*:", temp_leaks[:5] or "keine")
    if not args.keep:
        shutil.rmtree(data_dir, ignore_errors=True)
        print("Datenordner geloescht")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
