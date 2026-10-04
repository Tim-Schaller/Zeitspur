"""Erzeugt eine Demo-Datenbank mit synthetischen Eintraegen (gerenderte Fake-Screenshots + Text).

Nuetzlich, um Zeitstrahl und MCP-Server ohne stundenlange Aufnahme zu testen.

Aufruf (Datenordner ueber Umgebungsvariable, damit die echte Installation unberuehrt bleibt):
  $env:ZEITSPUR_DATA_DIR = "$env:TEMP\\ZeitspurDemo"
  .venv\\Scripts\\python tools\\seed_demo_db.py --days 3
"""
from __future__ import annotations

import argparse
import io
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zeitspur import timeutil  # noqa: E402
from zeitspur.config import Config, ensure_data_dir, is_first_run, key_path, save_config  # noqa: E402
from zeitspur.crypto import load_or_create_key  # noqa: E402
from zeitspur.storage import Storage  # noqa: E402

APPS = [
    ("OUTLOOK.EXE", ["Posteingang - max@example.com - Outlook", "AW: Serverumzug Termin - Nachricht (HTML)", "Kalender - Outlook"],
     ["Betreff: Serverumzug am Freitag", "Von: Müller, Anna", "Rechnungsnummer 4711 bitte prüfen", "Meeting 12:00 Raum B2"]),
    ("WINWORD.EXE", ["Angebot_Kunde_XY.docx - Word", "Protokoll Serverumzug.docx - Word"],
     ["Angebot für die Migration der Fileserver", "Positionen: 1. Analyse 2. Umzug 3. Test", "Gesamtsumme 12.450,00 EUR"]),
    ("Teams.exe", ["Chat | Microsoft Teams", "Besprechung Serverumzug | Microsoft Teams"],
     ["Anna Müller: Können wir den Termin verschieben?", "Max: Ja, Donnerstag 14 Uhr passt", "Agenda: Backup, DNS, Freigaben"]),
    ("firefox.exe", ["Windows Recall – Wikipedia — Mozilla Firefox", "SQLCipher Documentation — Mozilla Firefox"],
     ["Recall is a feature that captures snapshots", "PRAGMA key and cipher_page_size", "Verschlüsselung mit AES-256"]),
    ("Code.exe", ["storage.py - Zeitspur - Visual Studio Code", "capture.py - Zeitspur - Visual Studio Code"],
     ["def insert_entry(self, ts_start, ts_end):", "class CaptureLoop(threading.Thread):", "TODO: Retention testen"]),
]


def render_fake_screenshot(app: str, title: str, lines: list[str], width: int = 1280, height: int = 800) -> tuple[bytes, str]:
    rng = random.Random(hash((app, title)) & 0xFFFF)  # eigener Generator: globalen Zufall nicht zuruecksetzen
    bg = (rng.randint(225, 245), rng.randint(225, 245), rng.randint(230, 250))
    img = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 22)
        small = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 16)
    except OSError:
        font = small = ImageFont.load_default()
    d.rectangle([0, 0, width, 36], fill=(60, 64, 72))
    d.text((12, 8), f"{title}", fill=(240, 240, 240), font=small)
    d.rectangle([0, 36, 220, height], fill=(min(255, bg[0] - 20), min(255, bg[1] - 20), min(255, bg[2] - 15)))
    y = 70
    text_lines = [f"{app.replace('.exe', '').replace('.EXE', '')}", *lines]
    for line in text_lines:
        d.text((250, y), line, fill=(25, 25, 30), font=font)
        y += 40
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=70)
    return buf.getvalue(), "\n".join(lines)


def seed(storage: Storage, days: int, start_hour: int = 8, end_hour: int = 17) -> int:
    count = 0
    today = datetime.now().date()
    for day_offset in range(days, 0, -1):
        day = today - timedelta(days=day_offset - 1)
        t = datetime(day.year, day.month, day.day, start_hour, random.randint(0, 20))
        end = datetime(day.year, day.month, day.day, end_hour, 0)
        while t < end:
            app, titles, lines = random.choice(APPS)
            title = random.choice(titles)
            block_minutes = random.randint(3, 25)
            block_end = min(end, t + timedelta(minutes=block_minutes))
            cur = t
            while cur < block_end:
                webp, text = render_fake_screenshot(app, title, random.sample(lines, k=min(3, len(lines))))
                step = random.randint(20, 90)
                ts_start = timeutil.to_ms(cur)
                ts_end = timeutil.to_ms(min(block_end, cur + timedelta(seconds=step)))
                eid = storage.insert_entry(ts_start=ts_start, ts_end=ts_end, monitor_id=1, process_name=app,
                                           window_title=title, exe_path=None, width=1280, height=800, webp=webp)
                storage.set_ocr_result(eid, text, random.uniform(80, 97))
                count += 1
                cur += timedelta(seconds=step)
            t = block_end + timedelta(minutes=random.choice([0, 0, 1, 5, 30]))
    return count


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=3)
    args = p.parse_args()
    data_dir = ensure_data_dir()
    cfg = Config()
    if is_first_run():
        save_config(cfg)
        print(f"config.yaml angelegt in {data_dir}")
    key = load_or_create_key(key_path())
    storage = Storage(cfg.resolved_db_path, key)
    n = seed(storage, args.days)
    st = storage.stats()
    storage.close()
    print(f"{n} Eintraege erzeugt -> {cfg.resolved_db_path} ({st['db_size_bytes'] / 1e6:.1f} MB, gesamt {st['entries']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
