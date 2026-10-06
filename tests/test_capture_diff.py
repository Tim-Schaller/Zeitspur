import threading

import pytest
from PIL import Image, ImageDraw

from zeitspur import timeutil, winutil
from zeitspur.capture import (CaptureLoop, CaptureState, FrameDiff, downscale_for_diff, encode_png_gray,
                                 encode_webp, frame_difference)
from zeitspur.config import Config

SIZE = (1920, 1200)
MON = {"left": 0, "top": 0, "width": 1920, "height": 1200}


def screen(color=(245, 245, 245)) -> Image.Image:
    return Image.new("RGB", SIZE, color)


def with_text_lines(img: Image.Image, lines: int, y0: int = 100) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    for i in range(lines):
        y = y0 + i * 28
        d.rectangle([40, y, 640, y + 18], fill=(20, 20, 20))  # simuliert eine Textzeile (600x18 px)
    return out


def striped(offset: int = 0) -> Image.Image:
    img = screen()
    d = ImageDraw.Draw(img)
    for y in range(0, 1200, 40):
        d.rectangle([0, (y + offset) % 1200, 1920, (y + offset) % 1200 + 12], fill=(60, 60, 60))
    return img


def test_identical_frames_are_unchanged():
    fd = FrameDiff(0.01)
    a = fd.prepare(screen())
    assert fd.ratio(1, a) == 1.0  # ohne Referenz gilt alles als neu
    fd.commit(1, a)
    changed, ratio = fd.is_changed(1, fd.prepare(screen()))
    assert not changed and ratio == 0.0


def test_cursor_blink_is_noise():
    fd = FrameDiff(0.01)
    fd.commit(1, fd.prepare(screen()))
    blink = screen()
    ImageDraw.Draw(blink).rectangle([300, 300, 312, 324], fill=(0, 0, 0))
    changed, ratio = fd.is_changed(1, fd.prepare(blink))
    assert not changed and ratio < 0.002


def test_typing_accumulates_against_stored_frame():
    fd = FrameDiff(0.01)
    base = screen()
    fd.commit(1, fd.prepare(base))
    one_line = with_text_lines(base, 1)
    changed1, r1 = fd.is_changed(1, fd.prepare(one_line))
    assert not changed1 and 0.002 < r1 < 0.01  # eine Zeile allein liegt unter der Schwelle
    three_lines = with_text_lines(base, 3)
    changed3, r3 = fd.is_changed(1, fd.prepare(three_lines))
    assert changed3 and r3 > r1  # Referenz bleibt der gespeicherte Frame -> Aenderungen summieren sich


def test_scrolling_is_a_change():
    fd = FrameDiff(0.01)
    fd.commit(1, fd.prepare(striped(0)))
    changed, ratio = fd.is_changed(1, fd.prepare(striped(20)))
    assert changed and ratio > 0.3


def test_resolution_change_and_reset():
    fd = FrameDiff(0.01)
    fd.commit(1, fd.prepare(screen()))
    other = Image.new("RGB", (1280, 800), (245, 245, 245))
    assert fd.ratio(1, fd.prepare(other)) == 0.0  # gleiche Downscale-Breite, gleicher Inhalt
    assert frame_difference(Image.new("L", (10, 10)), Image.new("L", (12, 10))) == 1.0
    fd.reset(1)
    assert fd.ratio(1, fd.prepare(screen())) == 1.0


def test_downscale_geometry():
    small = downscale_for_diff(screen(), 160)
    assert small.size == (160, 100) and small.mode == "L"


def test_encoders_stay_in_memory(tmp_path):
    big = Image.new("RGB", (3840, 2400), (10, 200, 10))
    webp, w, h = encode_webp(big, 1920, 75)
    assert (w, h) == (1920, 1200)
    assert webp[:4] == b"RIFF" and webp[8:12] == b"WEBP"
    png = encode_png_gray(screen())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []  # keine Bilddatei geschrieben


class FakeOcr:
    def __init__(self, accept: bool = True):
        self.jobs: list[tuple[int, bytes]] = []
        self.accept = accept

    def submit(self, entry_id, image_bytes):
        self.jobs.append((entry_id, image_bytes))
        return self.accept


def make_loop(storage, monkeypatch, cfg: Config | None = None):
    cfg = cfg or Config(capture_interval_seconds=5, change_threshold=0.01)
    stop = threading.Event()
    states: list[CaptureState] = []
    ocr = FakeOcr()
    loop = CaptureLoop(cfg, storage, ocr, stop, on_state=lambda s, d: states.append(s), start_delay=0)
    frames = {"img": screen()}
    fg = {"info": winutil.WindowInfo(1, "Dokument - Word", 100, "WINWORD.EXE", r"C:\Office\WINWORD.EXE", (100, 100, 900, 700))}
    monkeypatch.setattr(loop, "_screenshots", lambda: [(1, MON, frames["img"])])
    monkeypatch.setattr(loop, "current_foreground", lambda: fg["info"])
    monkeypatch.setattr(winutil, "is_session_locked", lambda: False)
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(winutil, "free_disk_bytes", lambda p: 10 ** 12)
    clock = {"ms": timeutil.to_ms(__import__("datetime").datetime(2026, 9, 10, 9, 0, 0))}
    monkeypatch.setattr(timeutil, "now_ms", lambda: clock["ms"])
    return loop, frames, fg, clock, states, ocr


def entries(storage):
    return storage.entries_between(0, 10 ** 15)


def test_capture_loop_stores_extends_and_splits(storage, monkeypatch):
    loop, frames, fg, clock, states, ocr = make_loop(storage, monkeypatch)
    loop.tick()
    rows = entries(storage)
    assert len(rows) == 1 and rows[0]["process_name"] == "WINWORD.EXE" and rows[0]["window_title"] == "Dokument - Word"
    assert rows[0]["width"] == 1920 and loop.state == CaptureState.RECORDING
    assert len(ocr.jobs) == 1 and ocr.jobs[0][1][:8] == b"\x89PNG\r\n\x1a\n"
    assert storage.get_image(rows[0]["id"])[:4] == b"RIFF"
    # unveraenderter Frame -> kein neuer Eintrag, ts_end wird (gepuffert) verlaengert
    clock["ms"] += 5000
    loop.tick()
    assert len(entries(storage)) == 1
    loop.flush_extends()
    assert storage.get_entry(rows[0]["id"])["ts_end"] == clock["ms"]
    # Inhalt aendert sich -> neuer Eintrag
    clock["ms"] += 5000
    frames["img"] = striped(0)
    loop.tick()
    assert len(entries(storage)) == 2
    # gleicher Inhalt, aber anderes Vordergrundfenster -> neuer Eintrag
    clock["ms"] += 5000
    fg["info"] = winutil.WindowInfo(2, "Posteingang - Outlook", 200, "OUTLOOK.EXE", None, (100, 100, 900, 700))
    loop.tick()
    rows = entries(storage)
    assert len(rows) == 3 and rows[-1]["process_name"] == "OUTLOOK.EXE"
    # Keyframe nach max_block_minutes
    clock["ms"] += loop.cfg.max_block_ms
    loop.tick()
    assert len(entries(storage)) == 4
    assert loop.entries_created == 4 and loop.frames_seen == 5


def test_capture_loop_states(storage, monkeypatch):
    loop, frames, fg, clock, states, ocr = make_loop(storage, monkeypatch)
    loop.tick()
    assert loop.state == CaptureState.RECORDING
    # Deny-Liste (Prozess)
    fg["info"] = winutil.WindowInfo(3, "Datenbank.kdbx - KeePass", 300, "KeePass.exe", None, (0, 0, 800, 600))
    clock["ms"] += 5000
    loop.tick()
    assert loop.state == CaptureState.EXCLUDED and len(entries(storage)) == 1
    # Deny-Liste (Titel)
    fg["info"] = winutil.WindowInfo(4, "Bank - Inkognito - Firefox", 301, "firefox.exe", None, (0, 0, 800, 600))
    loop.tick()
    assert loop.state == CaptureState.EXCLUDED
    # zurueck -> Bloecke wurden geschlossen, also neuer Eintrag trotz gleichem Bild
    fg["info"] = winutil.WindowInfo(1, "Dokument - Word", 100, "WINWORD.EXE", None, (0, 0, 800, 600))
    clock["ms"] += 5000
    loop.tick()
    assert loop.state == CaptureState.RECORDING and len(entries(storage)) == 2
    # Pause
    loop.set_paused(True)
    loop.tick()
    assert loop.state == CaptureState.PAUSED and len(entries(storage)) == 2
    loop.set_paused(False)
    # gesperrt / inaktiv / kein Platz
    monkeypatch.setattr(winutil, "is_session_locked", lambda: True)
    loop.tick()
    assert loop.state == CaptureState.LOCKED
    monkeypatch.setattr(winutil, "is_session_locked", lambda: False)
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 10_000.0)
    loop.tick()
    assert loop.state == CaptureState.IDLE
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(winutil, "free_disk_bytes", lambda p: 0)
    loop.tick()
    assert loop.state == CaptureState.NO_DISK
    assert len(entries(storage)) == 2
    assert CaptureState.RECORDING in states and CaptureState.EXCLUDED in states


def test_capture_loop_runtime_exclusion(storage, monkeypatch):
    loop, frames, fg, clock, states, ocr = make_loop(storage, monkeypatch)
    loop.add_exclusion("WINWORD.EXE")
    loop.tick()
    assert loop.state == CaptureState.EXCLUDED
    assert "WINWORD\\.EXE" in loop.cfg.excluded_process_regex


def test_capture_loop_survives_screenshot_errors(storage, monkeypatch):
    loop, frames, fg, clock, states, ocr = make_loop(storage, monkeypatch)

    def boom():
        raise RuntimeError("Screenshot fehlgeschlagen: kaputt")

    monkeypatch.setattr(loop, "_screenshots", boom)
    stop = loop.stop_event
    loop.cfg.capture_interval_seconds = 2
    loop.start()
    import time

    time.sleep(0.8)
    stop.set()
    loop.join(5)
    assert not loop.is_alive()
    assert loop.last_error and "kaputt" in loop.last_error
    assert loop.state == CaptureState.STOPPED


def test_capture_labels_each_monitor_with_its_own_window(storage, monkeypatch):
    """Auf einem Monitor ohne Vordergrundfenster wird das oberste Fenster dieses Monitors verwendet,
    nicht das Vordergrundfenster (behebt die Fehlbeschriftung mehrerer Monitore)."""
    cfg = Config(capture_interval_seconds=5, change_threshold=0.01)
    stop = threading.Event()
    loop = CaptureLoop(cfg, storage, None, stop, start_delay=0)
    mon1 = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    mon2 = {"left": 1920, "top": 0, "width": 1920, "height": 1080}
    monkeypatch.setattr(loop, "_screenshots", lambda: [(1, mon1, screen((250, 250, 250))), (2, mon2, screen((10, 40, 90)))])
    # Vordergrund liegt auf Monitor 1
    fg = winutil.WindowInfo(1, "Dokument - Word", 100, "WINWORD.EXE", None, (100, 100, 900, 700))
    monkeypatch.setattr(loop, "current_foreground", lambda: fg)
    # oberstes Fenster auf Monitor 2: Outlook
    monkeypatch.setattr(winutil, "top_window_per_monitor",
                        lambda monitors: {2: winutil.WindowInfo(2, "Posteingang - Outlook", 200, "OUTLOOK.EXE", None, (1920, 0, 3840, 1080))})
    clock = {"ms": timeutil.to_ms(__import__("datetime").datetime(2026, 9, 10, 9, 0, 0))}
    monkeypatch.setattr(timeutil, "now_ms", lambda: clock["ms"])
    monkeypatch.setattr(winutil, "is_session_locked", lambda: False)
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(winutil, "free_disk_bytes", lambda p: 10 ** 12)
    loop.tick()
    rows = storage.entries_between(0, 10 ** 15)
    by_mon = {r["monitor_id"]: r for r in rows}
    assert by_mon[1]["process_name"] == "WINWORD.EXE"
    assert by_mon[2]["process_name"] == "OUTLOOK.EXE" and by_mon[2]["window_title"] == "Posteingang - Outlook"


def test_capture_per_monitor_deny_skips_only_that_monitor(storage, monkeypatch):
    cfg = Config(capture_interval_seconds=5, change_threshold=0.01, excluded_process_regex=["KeePass.*"])
    stop = threading.Event()
    loop = CaptureLoop(cfg, storage, None, stop, start_delay=0)
    mon1 = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    mon2 = {"left": 1920, "top": 0, "width": 1920, "height": 1080}
    monkeypatch.setattr(loop, "_screenshots", lambda: [(1, mon1, screen((250, 250, 250))), (2, mon2, screen((10, 40, 90)))])
    fg = winutil.WindowInfo(1, "Dokument - Word", 100, "WINWORD.EXE", None, (100, 100, 900, 700))
    monkeypatch.setattr(loop, "current_foreground", lambda: fg)
    monkeypatch.setattr(winutil, "top_window_per_monitor",
                        lambda monitors: {2: winutil.WindowInfo(2, "Tresor.kdbx - KeePass", 200, "KeePass.exe", None, (1920, 0, 3840, 1080))})
    clock = {"ms": timeutil.to_ms(__import__("datetime").datetime(2026, 9, 10, 9, 0, 0))}
    monkeypatch.setattr(timeutil, "now_ms", lambda: clock["ms"])
    monkeypatch.setattr(winutil, "is_session_locked", lambda: False)
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(winutil, "free_disk_bytes", lambda p: 10 ** 12)
    loop.tick()
    rows = storage.entries_between(0, 10 ** 15)
    assert [r["monitor_id"] for r in rows] == [1]  # Monitor 2 (KeePass) wird uebersprungen, Monitor 1 nicht
    assert loop.state == CaptureState.RECORDING


def test_capture_deny_matches_visible_window_below_top(storage, monkeypatch):
    """Eine ausgeschlossene App, die sichtbar aber NICHT das oberste Fenster des Monitors ist, spart den
    Monitor trotzdem aus (fruehere Luecke: nur das oberste/fokussierte Fenster wurde geprueft)."""
    cfg = Config(capture_interval_seconds=5, change_threshold=0.01, excluded_process_regex=["KeePass.*"])
    stop = threading.Event()
    loop = CaptureLoop(cfg, storage, None, stop, start_delay=0)
    mon1 = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    mon2 = {"left": 1920, "top": 0, "width": 1920, "height": 1080}
    monkeypatch.setattr(loop, "_screenshots", lambda: [(1, mon1, screen((250, 250, 250))), (2, mon2, screen((10, 40, 90)))])
    fg = winutil.WindowInfo(1, "Dokument - Word", 100, "WINWORD.EXE", None, (100, 100, 900, 700))
    monkeypatch.setattr(loop, "current_foreground", lambda: fg)
    # Oberstes Fenster auf Monitor 2 ist ein Browser - NICHT ausgeschlossen ...
    browser = winutil.WindowInfo(2, "Seite - Firefox", 200, "firefox.exe", None, (1920, 0, 3840, 1080))
    monkeypatch.setattr(winutil, "top_window_per_monitor", lambda monitors: {2: browser})
    # ... aber darunter liegt sichtbar ein ausgeschlossenes KeePass-Fenster.
    keepass = winutil.WindowInfo(2, "Tresor.kdbx - KeePass", 201, "KeePass.exe", None, (1960, 60, 3000, 900))
    monkeypatch.setattr(winutil, "visible_windows_per_monitor", lambda monitors: {1: [], 2: [browser, keepass]})
    clock = {"ms": timeutil.to_ms(__import__("datetime").datetime(2026, 9, 10, 9, 0, 0))}
    monkeypatch.setattr(timeutil, "now_ms", lambda: clock["ms"])
    monkeypatch.setattr(winutil, "is_session_locked", lambda: False)
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(winutil, "free_disk_bytes", lambda p: 10 ** 12)
    loop.tick()
    rows = storage.entries_between(0, 10 ** 15)
    assert [r["monitor_id"] for r in rows] == [1]  # Monitor 2 trotz unauffaelligem oberstem Fenster uebersprungen


def test_encode_webp_bleibt_schlank_und_lesbar():
    """Der schnellere Kodiermodus darf das Bild nicht unbrauchbar oder riesig machen."""
    import io

    from PIL import Image

    from zeitspur.capture import WEBP_METHOD, encode_webp
    assert WEBP_METHOD == 2
    img = Image.new("RGB", (2400, 1400), (20, 40, 80))
    for x in range(0, 2400, 40):
        for y in range(0, 1400, 40):
            img.putpixel((x, y), (240, 240, 100))
    daten, breite, hoehe = encode_webp(img, 1920, 75)
    assert (breite, hoehe) == (1920, 1120)              # auf max_image_width skaliert
    zurueck = Image.open(io.BytesIO(daten))
    assert zurueck.size == (1920, 1120) and zurueck.format == "WEBP"
    assert len(daten) < 1920 * 1120 * 3 // 20           # deutlich kleiner als roh


def test_screenshots_lesen_die_monitore_nach_dem_andocken_neu_ein(storage, monkeypatch):
    """mss merkt sich die Monitore beim Anlegen. Nach dem Andocken blieb es deshalb bis zum naechsten Programmstart
    bei einem Monitor - aufgenommen wurde nur das Rechteck des Notebook-Bildschirms."""
    import mss

    from zeitspur import capture
    from zeitspur.capture import OpenBlock

    notebook = ((0, 0, 1920, 1200),)
    docked = ((-1920, 0, 0, 1080), (2560, 0, 4480, 1080), (0, 0, 2560, 1440))
    layout = {"now": notebook}
    created = []

    class FakeMss:
        def __init__(self):
            self.monitors = [{}] + [{"left": l, "top": t, "width": r - l, "height": b - t}
                                    for l, t, r, b in layout["now"]]
            self.closed = False
            created.append(self)

        def grab(self, mon):
            return (mon["width"] // 40, mon["height"] // 40)   # nur die Groesse zaehlt

        def close(self):
            self.closed = True

    monkeypatch.setattr(mss, "mss", FakeMss)
    monkeypatch.setattr(capture, "shot_to_image", lambda size: Image.new("RGB", size))
    monkeypatch.setattr(winutil, "monitor_layout", lambda: layout["now"])
    loop = CaptureLoop(Config(), storage, None, threading.Event(), start_delay=0)

    assert [m["width"] for _, m, _ in loop._screenshots()] == [1920]
    assert len(loop._screenshots()) == 1 and len(created) == 1          # unveraendert: nicht neu anlegen
    entry_id = storage.insert_entry(ts_start=1000, ts_end=1000, monitor_id=1, process_name="WINWORD.EXE",
                                    window_title="Dokument", exe_path=None, width=8, height=8,
                                    webp=encode_webp(Image.new("RGB", (8, 8)), 1920, 75)[0])
    loop._blocks[1] = OpenBlock(entry_id, 1000, 5000, "WINWORD.EXE", "Dokument")

    layout["now"] = docked                                              # Dockingstation: drei Monitore
    frames = loop._screenshots()
    assert [(i, m["left"], m["width"]) for i, m, _ in frames] == [(1, -1920, 1920), (2, 2560, 1920), (3, 0, 2560)]
    assert len(created) == 2 and created[0].closed
    assert loop._blocks == {}                                           # Monitor 1 ist jetzt ein anderer Bildschirm

    layout["now"] = notebook                                            # abgedockt: nichts Schwarzes weiter aufnehmen
    assert len(loop._screenshots()) == 1 and len(created) == 3 and created[1].closed
