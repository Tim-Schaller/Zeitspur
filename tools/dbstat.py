"""Zeigt Statistiken der Zeitspur-Datenbank (nur lesend). Beruecksichtigt ZEITSPUR_DATA_DIR.

  .venv\\Scripts\\python tools\\dbstat.py [--days 7] [--search "Begriff"]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zeitspur import timeutil  # noqa: E402
from zeitspur.config import key_path, load_config  # noqa: E402
from zeitspur.crypto import unprotect_key  # noqa: E402
from zeitspur.storage import ReadOnlyStorage  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7, help="Tagesuebersicht fuer die letzten N Tage")
    p.add_argument("--search", help="FTS-Suche ausfuehren und Treffer zeigen")
    args = p.parse_args()
    cfg = load_config()
    kp = key_path()
    if not kp.exists() or not cfg.resolved_db_path.exists():
        print(f"Keine Datenbank/kein Schluessel unter {cfg.resolved_db_path.parent}")
        return 1
    store = ReadOnlyStorage(cfg.resolved_db_path, unprotect_key(kp.read_bytes()))
    st = store.stats()
    print(f"Datenbank : {cfg.resolved_db_path}")
    print(f"Eintraege : {st['entries']}  (OCR offen: {st['pending_ocr']})")
    print(f"Groesse   : {st['db_size_bytes'] / 1e6:.1f} MB  (Seiten {st['page_count']} x {st['page_size']}, frei {st['freelist_count']})")
    if st["oldest_ms"]:
        print(f"Zeitraum  : {timeutil.iso_local(st['oldest_ms'])}  ..  {timeutil.iso_local(st['newest_ms'])}")
    print(f"Wartung   : last_cleanup={store.get_meta('last_cleanup')}  freigegeben={store.get_meta('bytes_freed_total', '0')} Bytes")
    days = store.list_days()[-args.days:]
    for d in days:
        start, end = timeutil.day_bounds(timeutil.parse_date(d))
        apps = store.app_stats(start, end)
        top = ", ".join(f"{a['process_name']} {timeutil.human_duration(a['total_ms'])}" for a in apps[:4])
        print(f"  {d}: {store.count_between(start, end):5d} Eintraege | {top}")
    if args.search:
        for r in store.search(args.search, limit=10):
            print(f"  [{r['id']}] {timeutil.iso_local(r['ts_start'])} {r['process_name']} | {r['window_title'][:50]} | {' '.join(r['snippet'].split())[:90]}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
