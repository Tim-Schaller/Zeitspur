"""Gemeinsame Test-Hilfen."""
from __future__ import annotations

import io
import os

from PIL import Image

FAKE_WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + os.urandom(48)


def make_entry(storage, ts_start: int, ts_end: int | None = None, *, monitor_id: int = 1,
               process_name: str = "explorer.exe", window_title: str = "Fenster",
               ocr_text: str | None = None, webp: bytes | None = None, exe_path: str | None = None) -> int:
    entry_id = storage.insert_entry(
        ts_start=ts_start, ts_end=ts_end if ts_end is not None else ts_start + 5000,
        monitor_id=monitor_id, process_name=process_name, window_title=window_title,
        exe_path=exe_path, width=1920, height=1200, webp=webp or FAKE_WEBP)
    if ocr_text is not None:
        storage.set_ocr_result(entry_id, ocr_text, 90.0)
    return entry_id


def make_webp(width: int = 320, height: int = 200, color=(30, 120, 200), quality: int = 75) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=quality)
    return buf.getvalue()
